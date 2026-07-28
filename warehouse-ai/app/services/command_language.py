"""Deterministic, conservative parsing for common Korean warehouse commands.

The parser deliberately recognizes only explicit phrases.  It never validates an
identifier against operational state; that happens after the Snapshot is built.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Iterable

from app.models import (
    ClosedEdgeAssumption,
    CommandInterpretation,
    FixedRobotAssignment,
    HypotheticalEvent,
    InventoryQuantityFilter,
    InventoryOperationRequest,
    OptimizationWeights,
    TaskScheduleConstraint,
)
from app.services.scheduling import (
    parse_schedule_language,
    parse_planning_reference_time,
    parse_explicit_time_windows,
    resolve_warehouse_timezone,
    validate_dependency_graph,
)


OPTIMIZATION_FOCUS_MULTIPLIER = 5.0
OPTIMIZATION_PRIORITY_FIELDS: dict[str, str] = {
    "MINIMIZE_DISTANCE": "total_distance",
    "MINIMIZE_MAKESPAN": "makespan",
    "MINIMIZE_TARDINESS": "tardiness",
    "MINIMIZE_ENERGY": "energy",
    # MINIMIZE_ROBOTS is the existing public profile name.
    "MINIMIZE_ROBOTS": "robot_activation",
    "MINIMIZE_PLAN_CHANGE": "plan_change",
}
OPTIMIZATION_PRIORITY_PATTERNS: tuple[
    tuple[str, tuple[str, ...]], ...
] = (
    (
        "MINIMIZE_MAKESPAN",
        (
            r"전체\s*작업\s*완료\s*시간(?:을)?\s*최소화",
            r"전체\s*완료\s*시간(?:을)?\s*최소화",
            r"작업\s*완료\s*시간(?:을)?\s*최소화",
            r"총\s*소요\s*시간(?:을)?\s*(?:최소화|줄여|줄이|단축)",
            r"가장\s*빨리\s*끝내",
            r"최대한\s*빨리\s*완료",
            r"makespan(?:을)?\s*최소화",
            r"최대한\s*빨리",
            r"가장\s*빨리",
            r"완료\s*시간(?:을)?\s*최소화",
            r"시간\s*우선",
        ),
    ),
    (
        "MINIMIZE_DISTANCE",
        (
            r"이동\s*거리(?:를)?\s*(?:최소화|줄여|줄이)",
            r"최단\s*거리",
            r"거리\s*우선",
        ),
    ),
    (
        "MINIMIZE_TARDINESS",
        (
            r"납기\s*지연(?:을)?\s*(?:최소화|줄여|줄이)",
            r"tardiness(?:를|를)?\s*최소화",
            r"마감\s*준수",
            r"지연(?:을)?\s*최소화",
            r"마감\s*우선",
        ),
    ),
    (
        "MINIMIZE_ENERGY",
        (
            r"에너지\s*사용(?:을)?\s*(?:최소화|줄여|줄이)",
            r"에너지(?:를)?\s*최소화",
            r"에너지\s*우선",
            r"배터리\s*소모(?:를)?\s*최소화",
        ),
    ),
    (
        "MINIMIZE_ROBOTS",
        (
            r"사용(?:하는)?\s*로봇\s*수(?:를)?\s*최소화",
            r"로봇(?:을)?\s*(?:가장\s*)?적게\s*사용",
            r"최소\s*로봇",
            r"적은\s*로봇",
            r"로봇\s*수(?:를)?\s*최소화",
        ),
    ),
    (
        "MINIMIZE_PLAN_CHANGE",
        (
            r"기존\s*계획(?:을)?\s*(?:최대한\s*)?유지",
            r"변경(?:을)?\s*최소화",
            r"계획\s*변경(?:을)?\s*최소화",
        ),
    ),
)


def optimization_weights_for_priority(priority: str | None) -> OptimizationWeights:
    """Build deterministic weights for one or more named optimization profiles."""

    weights = OptimizationWeights()
    values = weights.model_dump()
    if not priority or priority in {"DEFAULT", "USER_DEFINED"}:
        return weights
    if priority == "BALANCE_DISTANCE_MAKESPAN":
        values["total_distance"] *= 2.0
        values["makespan"] *= 2.0
        return OptimizationWeights.model_validate(values)
    for name in priority.split("+"):
        field = OPTIMIZATION_PRIORITY_FIELDS.get(name)
        if field:
            values[field] = float(values[field]) * OPTIMIZATION_FOCUS_MULTIPLIER
    return OptimizationWeights.model_validate(values)


def parse_optimization_goal(
    text: str,
) -> tuple[str | None, OptimizationWeights, list[str]]:
    """Extract only explicit optimization goals and numeric weight overrides."""

    normalized = normalize_text(text)
    priorities: list[str] = []
    for priority, patterns in OPTIMIZATION_PRIORITY_PATTERNS:
        if any(re.search(pattern, normalized) for pattern in patterns):
            priorities.append(priority)

    priority = "+".join(priorities) if priorities else None
    weights = optimization_weights_for_priority(priority)
    values = weights.model_dump()

    if re.search(r"거리\s*(?:와|과)\s*(?:완료\s*)?시간\s*균형", normalized):
        priority = "BALANCE_DISTANCE_MAKESPAN"
        values = optimization_weights_for_priority(priority).model_dump()

    aliases = {
        "total_distance": ("total_distance", "거리"),
        "makespan": ("makespan", "완료시간"),
        "tardiness": ("tardiness", "지연"),
        "energy": ("energy", "에너지"),
        "robot_activation": ("robot_activation", "로봇수", "로봇 수"),
        "plan_change": ("plan_change", "계획변경", "계획 변경"),
        "charging_time": ("charging_time", "충전시간", "충전 시간"),
        "charger_wait": ("charger_wait", "충전대기", "충전 대기"),
        "charger_visit": ("charger_visit", "충전소방문", "충전소 방문"),
        "congestion": ("congestion", "혼잡"),
        "shared_resource_occupancy": (
            "shared_resource_occupancy",
            "공유자원점유",
            "공유 자원 점유",
        ),
        "unnecessary_charger_roundtrip": (
            "unnecessary_charger_roundtrip",
            "불필요한충전소왕복",
            "불필요한 충전소 왕복",
        ),
    }
    user_defined = False
    for field, names in aliases.items():
        for name in names:
            match = re.search(
                rf"{re.escape(name)}\s*(?:가중치)?\s*[:=]?\s*(\d+(?:\.\d+)?)",
                normalized,
            )
            if match:
                values[field] = float(match.group(1))
                user_defined = True
                break
    if user_defined:
        priority = "USER_DEFINED"

    ambiguous = []
    if not priority:
        ambiguous = [
            phrase
            for phrase in ("빠르게", "효율적으로", "최적으로", "가장 좋은")
            if phrase in normalized
        ]
    return priority, OptimizationWeights.model_validate(values), ambiguous


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", value).strip()


INVENTORY_QUANTITY_PATTERN = re.compile(
    r"(?:(?:상품|품목)\s*)?"
    r"(?P<item>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?:\s*(?:상품|품목))?\s*"
    r"(?P<quantity>[+-]?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>박스|boxes?|box|개|ea|낱개|팔레트|pallet|kg|g|ml|l)"
    r"(?:를|을|가|이|은|는|와|과)?(?=\s|,|\.|$)",
    re.IGNORECASE,
)
PARTIAL_FULFILLMENT_PHRASES = (
    "가능한 수량만 먼저",
    "가능한 수량만",
    "재고가 있는 만큼만",
    "부분 출고를 승인",
    "부분 출고하도록",
    "부분 출고해",
    "현재 가능한 수량부터",
    "나머지는 입고 후",
)
PARTIAL_FULFILLMENT_NEGATIONS = (
    "부분 출고하지 마",
    "부분 출고는 하지 마",
    "부분 출고 금지",
    "전체 수량만 출고",
    "전체 출고만",
)


def _inventory_clause_context(
    normalized: str,
    match_start: int,
    match_end: int,
) -> tuple[str, int]:
    """Return the punctuation-bounded clause containing one item quantity.

    Daily-plan commands commonly contain several independent inbound/outbound
    sentences.  Looking a fixed number of characters to either side lets the
    previous sentence's ``입고`` leak into the next sentence's ``출고``.
    Bound the operation decision to the local sentence instead.
    """

    left_candidates = [
        normalized.rfind(marker, 0, match_start)
        for marker in (".", "!", "?", ";", "。")
    ]
    left_boundary = max(left_candidates) + 1
    right_candidates = [
        index
        for marker in (".", "!", "?", ";", "。")
        for index in [normalized.find(marker, match_end)]
        if index >= 0
    ]
    right_boundary = min(right_candidates) if right_candidates else len(normalized)
    return normalized[left_boundary:right_boundary], left_boundary


def _operation_type_for_inventory_match(
    normalized: str,
    match_start: int,
    match_end: int,
) -> tuple[str | None, str, int]:
    """Resolve the explicit operation verb in the same local clause.

    Korean warehouse requests usually place the action verb after a list of
    items (``A 30 BOX와 B 20 BOX를 출고``).  Prefer the nearest following
    operation verb; when none follows, use the nearest preceding verb.
    """

    clause, clause_start = _inventory_clause_context(
        normalized, match_start, match_end
    )
    relative_start = match_start - clause_start
    relative_end = match_end - clause_start
    verbs = [
        (candidate.start(), candidate.group(0))
        for candidate in re.finditer(r"입고|출고", clause)
    ]
    if not verbs:
        # Some commands separate the item/quantity and its execution policy
        # with a full stop (for example, "E 30 BOX를 R2-03에 고정 배정해.
        # ... 출고 노드 2146으로 이동해").  Only when the local clause has
        # no operation verb at all, fall back to a small neighboring window.
        # Same-clause verbs always win, so a previous inbound sentence cannot
        # override an explicit outbound verb in the current sentence.
        window_start = max(0, match_start - 120)
        window_end = min(len(normalized), match_end + 160)
        window = normalized[window_start:window_end]
        window_relative_start = match_start - window_start
        window_relative_end = match_end - window_start
        window_verbs = [
            (candidate.start(), candidate.group(0))
            for candidate in re.finditer(r"입고|출고", window)
        ]
        following = [row for row in window_verbs if row[0] >= window_relative_end]
        if following:
            _, verb = min(following, key=lambda row: row[0] - window_relative_end)
            return ("INBOUND" if verb == "입고" else "OUTBOUND"), clause, clause_start
        preceding = [row for row in window_verbs if row[0] <= window_relative_start]
        if preceding:
            _, verb = max(preceding, key=lambda row: row[0])
            return ("INBOUND" if verb == "입고" else "OUTBOUND"), clause, clause_start
        return None, clause, clause_start
    following = [row for row in verbs if row[0] >= relative_end]
    if following:
        _, verb = min(following, key=lambda row: row[0] - relative_end)
    else:
        preceding = [row for row in verbs if row[0] <= relative_start]
        if not preceding:
            return None, clause, clause_start
        _, verb = max(preceding, key=lambda row: row[0])
    return ("INBOUND" if verb == "입고" else "OUTBOUND"), clause, clause_start


def _inventory_clock(
    text: str,
    *,
    reference_time: datetime,
    warehouse_timezone: str | None,
) -> datetime | None:
    """Resolve one explicit warehouse-local clock without changing scheduler rules."""

    match = re.search(
        r"(?P<date>오늘|내일|모레)?\s*"
        r"(?P<meridiem>오전|오후)?\s*"
        r"(?P<hour>\d{1,2})시(?:\s*(?P<minute>\d{1,2})분)?",
        text,
    )
    if not match:
        return None
    timezone, _, _ = resolve_warehouse_timezone(warehouse_timezone)
    local_reference = reference_time.astimezone(timezone)
    day_offset = {"오늘": 0, "내일": 1, "모레": 2}.get(
        match.group("date") or "오늘", 0
    )
    hour = int(match.group("hour"))
    meridiem = match.group("meridiem")
    if meridiem == "오후" and hour < 12:
        hour += 12
    elif meridiem == "오전" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return None
    local_value = datetime(
        local_reference.year,
        local_reference.month,
        local_reference.day,
        hour,
        int(match.group("minute") or 0),
        tzinfo=timezone,
    ) + timedelta(days=day_offset)
    # With no explicit relative date, operational commands use the nearest
    # future local clock.  A small grace window keeps a just-received command
    # at the same minute instead of rolling it to the next day.
    if not match.group("date") and local_value < local_reference - timedelta(minutes=1):
        local_value += timedelta(days=1)
    return local_value.astimezone(UTC)



def _bind_operation_time_windows(
    text: str,
    operations: list[InventoryOperationRequest],
    *,
    reference_time: datetime,
    warehouse_timezone: str | None,
) -> list[TaskScheduleConstraint]:
    """Bind explicit time windows to command-created inventory operations."""

    if not operations:
        return []
    windows = parse_explicit_time_windows(
        text,
        reference_time=reference_time,
        warehouse_timezone=warehouse_timezone,
    )
    if not windows:
        return []

    occurrence_index: dict[str, int] = {}
    constraints: list[TaskScheduleConstraint] = []
    for operation in operations:
        item_id = re.escape(operation.item_id)
        item_matches = list(
            re.finditer(
                rf"(?<![A-Za-z0-9_-]){item_id}(?:\s*(?:상품|품목))?(?![A-Za-z0-9_-])",
                text,
                re.IGNORECASE,
            )
        )
        index = occurrence_index.get(operation.item_id, 0)
        occurrence_index[operation.item_id] = index + 1
        item_position = (
            item_matches[min(index, len(item_matches) - 1)].start()
            if item_matches
            else len(text)
        )
        preceding = [row for row in windows if int(row["span_end"]) <= item_position]
        if preceding:
            selected = max(preceding, key=lambda row: int(row["span_end"]))
        else:
            selected = min(
                windows,
                key=lambda row: abs(int(row["span_start"]) - item_position),
            )
        earliest_start = selected["earliest_start"]
        latest_finish = selected["latest_finish"]
        constraints.append(
            TaskScheduleConstraint(
                work_id=operation.operation_id,
                earliest_start=earliest_start,
                latest_finish=latest_finish,
                time_constraint_type="HARD_WINDOW",
            )
        )
        if operation.operation_type == "OUTBOUND":
            operation.required_at = latest_finish
            operation.required_by = latest_finish
        else:
            operation.expected_arrival_at = earliest_start
    return constraints

def parse_inventory_operations(
    text: str,
    *,
    reference_time: datetime,
    warehouse_timezone: str | None,
) -> tuple[list[InventoryOperationRequest], list[str], list[str], bool]:
    """Extract explicit BOX operations conservatively.

    The function never converts a non-BOX unit and never creates a SQL order.
    Invalid quantities/units are returned as missing/ambiguous markers so the
    normal Clarification path can stop before optimization.
    """

    normalized = normalize_text(text)
    load_open_orders = bool(
        re.search(r"(?:오늘\s*)?주문과\s*입고\s*예정\s*데이터를\s*기준", normalized)
        or re.search(r"(?:open|미처리|진행\s*중인)\s*(?:입고|출고)?\s*주문", normalized)
    )
    partial = (
        _contains(normalized, PARTIAL_FULFILLMENT_PHRASES)
        and not _contains(normalized, PARTIAL_FULFILLMENT_NEGATIONS)
    )
    operations: list[InventoryOperationRequest] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    explicit_available = None
    available_match = re.search(
        r"검수\s*완료(?:\s*예정)?(?:은|이|가)?\s*([^,.\n]{0,30})",
        normalized,
    )
    if available_match:
        explicit_available = _inventory_clock(
            available_match.group(1),
            reference_time=reference_time,
            warehouse_timezone=warehouse_timezone,
        )

    for match in INVENTORY_QUANTITY_PATTERN.finditer(normalized):
        operation_type, context, clause_start = _operation_type_for_inventory_match(
            normalized, match.start(), match.end()
        )
        if operation_type is None:
            continue
        start = max(0, clause_start)
        unit = match.group("unit").upper()
        if unit in {"박스", "BOX", "BOXES"}:
            unit = "BOX"
        else:
            marker = (
                f"inventory_unit_confirmation:{match.group('item').upper()}:"
                f"{match.group('quantity')}:{match.group('unit')}"
            )
            missing.append(marker)
            ambiguous.append(match.group("unit"))
            continue
        raw_quantity = match.group("quantity")
        try:
            numeric = float(raw_quantity)
        except ValueError:
            numeric = 0
        if numeric <= 0 or not numeric.is_integer():
            missing.append(
                f"invalid_inventory_quantity:{match.group('item').upper()}:{raw_quantity}"
            )
            ambiguous.append(raw_quantity)
            continue

        prefix = normalized[max(0, start):match.start()]
        clock = _inventory_clock(
            prefix[-70:],
            reference_time=reference_time,
            warehouse_timezone=warehouse_timezone,
        )
        required_at = clock if operation_type == "OUTBOUND" else None
        expected_arrival_at = clock if operation_type == "INBOUND" else None
        operations.append(
            InventoryOperationRequest(
                operation_type=operation_type,
                item_id=match.group("item").upper(),
                quantity_boxes=int(numeric),
                unit="BOX",
                required_at=required_at,
                expected_arrival_at=expected_arrival_at,
                expected_available_at=(
                    explicit_available if operation_type == "INBOUND" else None
                ),
                priority=(
                    "EMERGENCY"
                    if _contains(context, ("최우선", "긴급"))
                    else "NORMAL"
                ),
                allow_partial_fulfillment=(
                    partial if operation_type == "OUTBOUND" else False
                ),
            )
        )
    # Some natural Korean commands place policy words between the item and
    # quantity, for example: "E상품 재고가 부족하면 가능한 수량만 부분
    # 출고하도록 150 BOX 출고".  The strict adjacent pattern above should
    # remain the primary parser, but when it finds nothing we can safely
    # recover one operation if the command contains exactly one explicitly
    # labelled item and exactly one BOX quantity.
    if not operations:
        labeled_items = extract_labeled_item_ids(text)
        quantity_matches = list(
            re.finditer(
                r"(?P<quantity>[+-]?\d+(?:\.\d+)?)\s*"
                r"(?P<unit>박스|boxes?|box)(?:를|을|가|이|은|는)?",
                normalized,
                re.IGNORECASE,
            )
        )
        if len(labeled_items) == 1 and len(quantity_matches) == 1:
            quantity_match = quantity_matches[0]
            raw_quantity = quantity_match.group("quantity")
            try:
                numeric = float(raw_quantity)
            except ValueError:
                numeric = 0
            context_start = max(0, quantity_match.start() - 120)
            context_end = min(len(normalized), quantity_match.end() + 80)
            context = normalized[context_start:context_end]
            inbound_index = context.rfind("입고")
            outbound_index = context.rfind("출고")
            if numeric <= 0 or not numeric.is_integer():
                missing.append(
                    f"invalid_inventory_quantity:{labeled_items[0]}:{raw_quantity}"
                )
                ambiguous.append(raw_quantity)
            elif inbound_index >= 0 or outbound_index >= 0:
                operation_type = (
                    "OUTBOUND"
                    if outbound_index >= inbound_index
                    else "INBOUND"
                )
                clock = _inventory_clock(
                    normalized[max(0, quantity_match.start() - 70):quantity_match.start()],
                    reference_time=reference_time,
                    warehouse_timezone=warehouse_timezone,
                )
                operations.append(
                    InventoryOperationRequest(
                        operation_type=operation_type,
                        item_id=labeled_items[0],
                        quantity_boxes=int(numeric),
                        unit="BOX",
                        required_at=clock if operation_type == "OUTBOUND" else None,
                        expected_arrival_at=(
                            clock if operation_type == "INBOUND" else None
                        ),
                        expected_available_at=(
                            explicit_available if operation_type == "INBOUND" else None
                        ),
                        priority=(
                            "EMERGENCY"
                            if _contains(context, ("최우선", "긴급"))
                            else "NORMAL"
                        ),
                        allow_partial_fulfillment=(
                            partial if operation_type == "OUTBOUND" else False
                        ),
                    )
                )

    return (
        operations,
        list(dict.fromkeys(missing)),
        list(dict.fromkeys(ambiguous)),
        load_open_orders,
    )


def _canonical(prefix: str, number: str, width: int) -> str:
    return f"{prefix}-{int(number):0{width}d}"


def canonical_robot_id(value: object) -> str:
    raw = str(value).strip().upper()
    # Preserve warehouse-qualified robot IDs such as R2-03.  The previous
    # implementation kept only the first number and collapsed R2-01, R2-02
    # and R2-03 into the same R-02 identifier.
    qualified = re.fullmatch(r"R\s*0*(\d+)\s*[-_]\s*0*(\d+)", raw, re.I)
    if qualified:
        warehouse_number, robot_number = qualified.groups()
        return f"R{int(warehouse_number)}-{int(robot_number):02d}"

    simple = re.fullmatch(
        r"(?:R(?:OBOT)?\s*[-_]?\s*|로봇\s*)0*(\d+)(?:\s*번)?",
        raw,
        re.I,
    )
    return _canonical("R", simple.group(1), 2) if simple else raw


def _robot_identifier_spans(text: str) -> list[tuple[int, int]]:
    patterns = (
        r"\bR\s*0*\d+\s*[-_]\s*0*\d+(?!\d)",
        r"\bR(?:OBOT)?\s*[-_]?\s*0*\d+(?!\d)",
        r"로봇\s*0*\d+\s*번?",
    )
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        spans.extend(match.span() for match in re.finditer(pattern, text, re.I))
    return spans


def _text_without_robot_identifiers(text: str) -> str:
    characters = list(text)
    for start, end in _robot_identifier_spans(text):
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _extract_edge_pairs(text: str) -> list[tuple[int, int]]:
    # A warehouse-qualified robot ID (for example R2-03) must never be
    # interpreted as an edge from node 2 to node 3.
    edge_text = _text_without_robot_identifiers(text)
    patterns = (
        # Compact edge notation: 2013-2014, 2013 -> 2014, 2013→2014
        r"(\d+)\s*(?:->|→|-)\s*(\d+)",
        # Korean natural order: 2013번 노드와 2014번 노드 사이 통로
        r"(\d+)\s*(?:번\s*)?노드\s*(?:와|과|및)\s*"
        r"(\d+)\s*(?:번\s*)?노드\s*(?:사이|간)",
        # Korean node-first order: 노드 2013과 노드 2014 사이 통로
        r"노드\s*0*(\d+)\s*(?:번\s*)?(?:와|과|및)\s*"
        r"노드\s*0*(\d+)\s*(?:번\s*)?(?:사이|간)",
        # Mixed order: 노드 2013과 2014번 노드 사이 통로
        r"노드\s*0*(\d+)\s*(?:번\s*)?(?:와|과|및)\s*"
        r"0*(\d+)\s*(?:번\s*)?노드\s*(?:사이|간)",
    )
    pairs: list[tuple[int, int]] = []
    for pattern in patterns:
        pairs.extend((int(a), int(b)) for a, b in re.findall(pattern, edge_text, re.I))
    return list(dict.fromkeys(pairs))


def _extract_closed_node_ids(text: str) -> list[int]:
    """Extract node IDs that are explicitly described as closed/unavailable.

    Supports both ``노드 2013`` and the natural Korean order
    ``2013번 노드`` while avoiding node mentions that merely identify the
    endpoints of an edge such as ``2013번 노드와 2014번 노드 사이 통로``.
    """

    node_text = _text_without_robot_identifiers(text)
    closure = r"(?:폐쇄(?:된|했다고|해|하고)?|차단(?:된|했다고|해|하고)?|막힘|막혔(?:다고)?|사용\s*불가)"
    particles = r"(?:을|를|이|가|은|는)?"
    patterns = (
        rf"노드\s*0*(\d+)\s*(?:번)?\s*{particles}\s*{closure}",
        rf"0*(\d+)\s*(?:번\s*)?노드\s*{particles}\s*{closure}",
    )
    values: list[int] = []
    for pattern in patterns:
        values.extend(int(value) for value in re.findall(pattern, node_text, re.I))
    return sorted(dict.fromkeys(values))


def _extract_excluded_node_ids(text: str) -> list[int]:
    values = set(_extract_closed_node_ids(text))
    node_text = _text_without_robot_identifiers(text)
    exclusion = r"(?:제외(?:하고|해|해줘|된)?|빼고|사용하지\s*않)"
    particles = r"(?:을|를|이|가|은|는)?"
    patterns = (
        rf"노드\s*0*(\d+)\s*(?:번)?\s*{particles}\s*{exclusion}",
        rf"0*(\d+)\s*(?:번\s*)?노드\s*{particles}\s*{exclusion}",
    )
    for pattern in patterns:
        values.update(int(value) for value in re.findall(pattern, node_text, re.I))
    return sorted(values)


def canonical_task_id(value: object) -> str:
    raw = str(value).strip().split(":", 1)[0]
    match = re.fullmatch(r"W(?:ORK)?\s*[-_]?\s*0*(\d+)", raw, re.I)
    return _canonical("W", match.group(1), 3) if match else raw.upper()


def extract_robot_ids(text: str) -> list[str]:
    values: list[str] = []
    occupied_spans: list[tuple[int, int]] = []

    qualified_pattern = r"\bR\s*0*(\d+)\s*[-_]\s*0*(\d+)(?!\d)"
    for match in re.finditer(qualified_pattern, text, re.I):
        values.append(canonical_robot_id(match.group(0)))
        occupied_spans.append(match.span())

    def overlaps_qualified(start: int, end: int) -> bool:
        return any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied_spans)

    simple_patterns = (
        r"\bR(?:OBOT)?\s*[-_]?\s*0*(\d+)(?!\d)",
        r"로봇\s*0*(\d+)\s*번",
        r"로봇\s*0*(\d+)(?!\d)",
    )
    for pattern in simple_patterns:
        for match in re.finditer(pattern, text, re.I):
            if overlaps_qualified(*match.span()):
                continue
            values.append(_canonical("R", match.group(1), 2))
    return sorted(set(values))


def extract_task_ids(text: str) -> list[str]:
    patterns = (
        r"\bw(?:ork)?\s*[-_]?\s*0*(\d+)",
        r"(?:작업|업무)\s*0*(\d+)\s*번",
    )
    values: list[str] = []
    for pattern in patterns:
        values.extend(_canonical("W", value, 3) for value in re.findall(pattern, text, re.I))
    named_patterns = (
        r"(?:작업|업무)\s+([a-z][a-z0-9_]*(?:-[a-z0-9]+)+)",
        r"\b([a-z][a-z0-9_]*(?:-[a-z0-9]+)+)\s*(?:작업|업무)",
    )
    for pattern in named_patterns:
        values.extend(
            canonical_task_id(value)
            for value in re.findall(pattern, text, re.I)
            if any(character.isdigit() for character in value)
        )
    return sorted(set(values))


def extract_labeled_item_ids(text: str) -> list[str]:
    """Extract compact item identifiers only when a product/item label is explicit."""
    labels = r"(?:상품|품목|제품)"
    values = {
        match.group(1).upper()
        for match in re.finditer(
            rf"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*)\s*{labels}",
            text,
            flags=re.IGNORECASE,
        )
    }
    values.update(
        match.group(1).upper()
        for match in re.finditer(
            rf"{labels}\s*([A-Za-z][A-Za-z0-9_-]*)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    for match in re.finditer(
        rf"(?<![A-Za-z0-9_-])([A-Za-z](?:\s*(?:와|및|,|and)\s*[A-Za-z])+?)\s*{labels}",
        text,
        flags=re.IGNORECASE,
    ):
        values.update(
            value.upper()
            for value in re.findall(r"[A-Za-z]", match.group(1))
        )
    return sorted(values)


def extract_inventory_quantity_filter(text: str) -> InventoryQuantityFilter | None:
    """Return the explicit stock-level predicate in a Korean inventory query."""
    match = re.search(
        r"(?:재고(?:가|는)?\s*)?(\d+)\s*(?:BOX|박스|개)?\s*(이하|미만|이상|초과)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    operator = {"이하": "LTE", "미만": "LT", "이상": "GTE", "초과": "GT"}[match.group(2)]
    return InventoryQuantityFilter(
        field="available_quantity_boxes",
        operator=operator,
        value=int(match.group(1)),
        unit="BOX",
    )


def inventory_query_requests_inbound(text: str) -> bool:
    return _contains(
        text,
        ("예정 입고", "입고 예정", "예정입고", "입고 여부", "입고 수량", "inbound"),
    )


def inventory_query_requests_storage(text: str) -> bool:
    return _contains(
        text,
        ("active storage", "storage 노드", "저장 노드 후보", "추가 보관 위치", "저장공간"),
    )


def extract_simulation_ids(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(
                r"(?:시뮬레이션|simulation)(?:\s*[_-]?\s*id)?\s*[:#]?\s*([a-z0-9][a-z0-9_-]{2,})",
                text,
                re.I,
            )
        )
    )


def extract_plan_versions(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(
                r"(?:계획\s*버전|plan_version)\s*[:#]?\s*([a-z0-9][a-z0-9_-]{2,})",
                text,
                re.I,
            )
        )
    )


def _contains(text: str, phrases: Iterable[str]) -> bool:
    return any(phrase in text for phrase in phrases)


SIMULATION_EXECUTION_PATTERNS = (
    r"(?:가상\s*)?시뮬레이션(?:을)?\s*(?:해\s*줘|돌려\s*줘|실행해\s*줘|하고)",
    r"테스트\s*해\s*줘",
    r"계획(?:을)?\s*검증\s*해\s*줘",
    r"(?:작업(?:을)?\s*)?수행\s*해\s*줘",
    r"실행\s*결과(?:를)?\s*보여\s*줘",
)

SIMULATION_QUERY_PATTERNS = (
    r"(?:기존|지난|과거|어제\s*수행한|저장된|이전|최근)[^\n]{0,40}시뮬레이션",
    r"시뮬레이션[^\n]{0,20}이력",
    r"최근[^\n]{0,20}시뮬레이션[^\n]{0,20}목록",
    r"이전\s*결과(?:를)?\s*다시\s*보여",
    r"(?:시뮬레이션|simulation)\s*[_-]?\s*id\b",
)


def _simulation_execution_requested(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in SIMULATION_EXECUTION_PATTERNS)


def _simulation_query_requested(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in SIMULATION_QUERY_PATTERNS)


def _execution_mode(text: str) -> tuple[str | None, list[str]]:
    if _contains(
        text,
        (
            "실제 실행",
            "로봇에 전송",
            "문제 없으면 적용",
            "검증 후 적용",
            "운영에 반영",
            "괜찮으면 실행",
        ),
    ):
        return "EXECUTE", []
    if _simulation_execution_requested(text):
        return "SIMULATE_ONLY", []
    if _contains(
        text,
        (
            "시뮬레이션",
            "가정해서 돌려",
            "가상으로 돌려",
            "실제 반영하지 말고",
            "미리 검증",
        ),
    ):
        return "SIMULATE_ONLY", []
    if _contains(
        text,
        (
            "계획만",
            "실행하지 말고 계획",
            "경로까지만",
            "계획을 만들어",
            "계획해줘",
        ),
    ):
        return "PLAN_ONLY", []
    ambiguous = [
        phrase
        for phrase in ("적용해봐", "처리해줘", "돌려줘")
        if phrase in text
    ]
    return None, ambiguous


def _optimization(text: str) -> tuple[str | None, OptimizationWeights, list[str]]:
    return parse_optimization_goal(text)


def _query_classification(text: str) -> tuple[str, str, str, list[str]] | None:
    # Reporting modifiers such as "상세하게 보여줘" do not turn a new
    # simulation request into a history query.  A clear execution verb wins.
    if _simulation_execution_requested(text):
        return None
    query_signal = _contains(
        text,
        ("조회", "보여줘", "알려줘", "목록", "상태", "이력", "몇 대", "몇 개", "근거"),
    )
    if not query_signal:
        return None
    explicit_simulation_query = (
        _simulation_query_requested(text) or bool(extract_simulation_ids(text))
    )
    # A generic "시뮬레이션 결과를 보여줘" without a past/stored-result
    # marker is treated as a new simulation request.  Only explicit history or
    # identifier lookup reaches SIMULATION_QUERY.
    if (
        _contains(text, ("시뮬레이션", "simulation"))
        and not explicit_simulation_query
    ):
        return None
    filters: list[str] = []
    if _contains(text, ("최적화 근거", "경로 근거", "evidence", "예약 근거", "충돌 방지 근거")):
        target, intent = "EVIDENCE", "EVIDENCE_QUERY"
    elif "초기화" in text and "이력" in text:
        target, intent = "RESET", "RESET_QUERY"
    elif "verification" in text or "검증 결과" in text:
        target, intent = "VERIFICATION", "VERIFICATION_QUERY"
    elif "재계획" in text and "이력" in text:
        target, intent = "REPLAN", "REPLAN_QUERY"
    elif explicit_simulation_query:
        target, intent = "SIMULATION", "SIMULATION_QUERY"
    elif _contains(text, ("활성 계획", "계획 버전", "현재 계획")):
        target, intent = "PLAN", "PLAN_QUERY"
    elif "로봇" in text or re.search(r"\br(?:obot)?[-_\s]*\d+", text):
        target, intent = "ROBOT", "ROBOT_QUERY"
        if _contains(text, ("배터리 부족", "저전력", "배터리 낮")):
            filters.append("LOW_BATTERY")
        if _contains(text, ("고장", "지연", "문제")):
            filters.append("FAILED_OR_DELAYED")
        if _contains(text, ("사용 가능", "가용")):
            filters.append("AVAILABLE")
    elif _contains(text, ("작업", "업무", "work")):
        target, intent = "WORK", "WORK_QUERY"
        status_filters = (
            ("EXECUTING", ("실행 중", "진행 중")),
            ("PLANNED", ("계획된", "예정")),
            ("DELAYED", ("지연",)),
            ("UNASSIGNED", ("미배정", "배정되지 않은")),
        )
        filters.extend(name for name, phrases in status_filters if _contains(text, phrases))
    elif _contains(text, ("재고", "품목", "lot")):
        target, intent = "INVENTORY", "INVENTORY_QUERY"
    elif _contains(text, ("지도", "노드", "통로", "구역", "경로")):
        target, intent = "MAP", "MAP_QUERY"
    elif _contains(text, ("시스템", "연결 상태")):
        target, intent = "SYSTEM", "SYSTEM_QUERY"
    else:
        return None

    if "이력" in text:
        action = "HISTORY"
    elif _contains(text, ("몇 대", "몇 개", "수량", "개수")):
        action = "COUNT"
    elif _contains(text, ("상세", "특정")) or extract_robot_ids(text) or extract_task_ids(text):
        action = "DETAIL"
    elif "상태" in text or filters:
        action = "STATUS"
    else:
        action = "LIST"
    return target, intent, action, filters


def _extract_battery_overrides(text: str) -> list[tuple[str, float]]:
    """Extract explicit per-robot hypothetical battery percentages.

    A percentage is accepted only when it is tied to a concrete robot ID and
    the sentence explicitly marks it as an assumption/simulation value.  This
    prevents a minimum-battery threshold such as ``20%`` from being mistaken
    for the robot's current battery.
    """

    pattern = re.compile(
        r"(?P<robot>\bR\s*0*\d+\s*[-_]\s*0*\d+(?!\d))"
        r"\s*(?:의)?\s*배터리(?:가|를|는|은)?"
        r"[^%\n]{0,36}?"
        r"(?P<percent>\d+(?:\.\d+)?)\s*%"
        r"\s*(?:(?:라고|로)\s*)?(?:가정|설정)",
        re.IGNORECASE,
    )
    values: dict[str, float] = {}
    for match in pattern.finditer(text):
        percent = float(match.group("percent"))
        if 0.0 <= percent <= 100.0:
            values[canonical_robot_id(match.group("robot"))] = percent
    return sorted(values.items())


def _extract_labeled_nodes(text: str) -> dict[str, list[int]]:
    """Extract explicitly labelled operational nodes by role.

    Inbound commands use an INBOUND node as the pickup source and a STORAGE
    node as the dropoff destination.  Keeping the labels separate avoids the
    previous global-target ambiguity where ``저장 노드 2088`` was lost.
    """

    node_text = _text_without_robot_identifiers(text)
    labelled_patterns = (
        ("OUTBOUND", r"출고\s*(?:장|지점)?\s*노드\s*(?:인|는|을|를)?\s*0*(\d+)"),
        ("OUTBOUND", r"0*(\d+)\s*(?:번\s*)?출고\s*(?:장|지점)?\s*노드"),
        ("INBOUND", r"입고\s*(?:장|지점)?\s*노드\s*(?:인|는|을|를)?\s*0*(\d+)"),
        ("INBOUND", r"0*(\d+)\s*(?:번\s*)?입고\s*(?:장|지점)?\s*노드"),
        ("STORAGE", r"저장\s*(?:공간|구역|장소)?\s*노드\s*(?:인|는|을|를|에)?\s*0*(\d+)"),
        ("STORAGE", r"0*(\d+)\s*(?:번\s*)?저장\s*(?:공간|구역|장소)?\s*노드"),
        ("DESTINATION", r"목적지\s*노드\s*(?:인|는|을|를|에)?\s*0*(\d+)"),
        ("DESTINATION", r"0*(\d+)\s*(?:번\s*)?목적지\s*노드"),
    )
    result: dict[str, list[int]] = {
        "OUTBOUND": [],
        "INBOUND": [],
        "STORAGE": [],
        "DESTINATION": [],
    }
    for candidate_type, pattern in labelled_patterns:
        result[candidate_type].extend(
            int(value) for value in re.findall(pattern, node_text, re.I)
        )
    return {
        key: sorted(dict.fromkeys(values))
        for key, values in result.items()
    }


def _extract_explicit_target_nodes(text: str) -> tuple[list[int], str | None]:
    """Extract the best generic destination while preserving old behavior."""

    labelled = _extract_labeled_nodes(text)
    for node_type in ("STORAGE", "OUTBOUND", "INBOUND", "DESTINATION"):
        if labelled[node_type]:
            return labelled[node_type], node_type
    return [], None


def _hypothetical_events(text: str, robots: list[str], tasks: list[str]) -> list[HypotheticalEvent]:
    events: list[HypotheticalEvent] = []

    def add(event_type: str, target_ids: list[str] | None = None, **parameters: object) -> None:
        events.append(
            HypotheticalEvent(
                event_type=event_type,
                target_ids=target_ids or [],
                parameters=parameters,
            )
        )

    if "고장" in text and (robots or "로봇" in text):
        add("ROBOT_FAILURE", robots)
    battery_overrides = _extract_battery_overrides(text)
    for robot_id, battery_percent in battery_overrides:
        add(
            "LOW_BATTERY",
            [robot_id],
            battery_percent=battery_percent,
        )
    if (
        not battery_overrides
        and _contains(text, ("배터리 부족", "배터리가 부족", "저전력"))
        and (robots or "로봇" in text)
    ):
        add("LOW_BATTERY", robots)
    node_values = _extract_closed_node_ids(text)
    if node_values:
        add("NODE_CLOSURE", [str(value) for value in node_values])
    edge_matches = _extract_edge_pairs(text)
    if edge_matches and _contains(text, ("통로", "간선", "폐쇄", "차단")):
        add("EDGE_CLOSURE", [f"{a}->{b}" for a, b in edge_matches])
    if _contains(text, ("충전소 사용 불가", "충전기 사용 불가", "충전소 고장")):
        add("CHARGER_UNAVAILABLE")
    if _contains(text, ("긴급 주문", "긴급 작업", "긴급 출고")):
        add("URGENT_ORDER", tasks)
    if _contains(text, ("작업 지연", "지연 가정")) or ("지연" in text and tasks):
        add("TASK_DELAY", tasks)
    if _contains(text, ("재고 부족", "재고가 부족")):
        add("INVENTORY_SHORTAGE")
    return events


def parse_deterministic_command(
    text: str,
    *,
    reference_time: datetime | None = None,
    warehouse_timezone: str | None = None,
) -> CommandInterpretation:
    raw = text.strip()
    normalized = normalize_text(raw)
    robots = extract_robot_ids(normalized)
    tasks = extract_task_ids(normalized)
    labeled_item_ids = extract_labeled_item_ids(raw)
    simulations = extract_simulation_ids(normalized)
    plan_versions = extract_plan_versions(normalized)
    base_reference_time = reference_time or datetime.now(UTC)
    planning_reference, planning_reference_errors = parse_planning_reference_time(
        raw,
        reference_time=base_reference_time,
        warehouse_timezone=warehouse_timezone,
    )
    effective_reference_time = (
        planning_reference.utc_at
        if planning_reference is not None
        else base_reference_time
    )
    schedule = parse_schedule_language(
        raw,
        reference_time=effective_reference_time,
        warehouse_timezone=warehouse_timezone,
    )
    (
        inventory_operations,
        inventory_missing,
        inventory_ambiguous,
        load_open_inventory_orders,
    ) = parse_inventory_operations(
        raw,
        reference_time=effective_reference_time,
        warehouse_timezone=warehouse_timezone,
    )
    operation_constraints = _bind_operation_time_windows(
        raw,
        inventory_operations,
        reference_time=effective_reference_time,
        warehouse_timezone=warehouse_timezone,
    )
    if operation_constraints:
        by_work = {row.work_id: row for row in schedule.constraints}
        by_work.update({row.work_id: row for row in operation_constraints})
        schedule.constraints = list(by_work.values())
        schedule.daily_schedule_requested = True
    _, dependency_errors = validate_dependency_graph(schedule.dependencies, tasks)
    mode, ambiguous_modes = _execution_mode(normalized)
    optimization_priority, weights, ambiguous_objectives = _optimization(normalized)
    query = _query_classification(normalized)
    comparison = "비교" in normalized or _contains(normalized, ("어느 게 좋은", "어느 것이 좋은"))
    labelled_nodes = _extract_labeled_nodes(normalized)
    operation_types = {row.operation_type for row in inventory_operations}
    source_node_ids: list[int] = []
    target_node_ids: list[int] = []
    target_node_type: str | None = None

    if operation_types == {"INBOUND"}:
        # An explicitly labelled inbound node is the pickup source.  A storage
        # or generic destination node is the inbound dropoff target.
        source_node_ids = list(labelled_nodes["INBOUND"])
        if labelled_nodes["STORAGE"]:
            target_node_ids = list(labelled_nodes["STORAGE"])
            target_node_type = "STORAGE"
        elif labelled_nodes["DESTINATION"]:
            target_node_ids = list(labelled_nodes["DESTINATION"])
            target_node_type = "DESTINATION"
    elif operation_types == {"OUTBOUND"}:
        if labelled_nodes["OUTBOUND"]:
            target_node_ids = list(labelled_nodes["OUTBOUND"])
            target_node_type = "OUTBOUND"
        elif labelled_nodes["DESTINATION"]:
            target_node_ids = list(labelled_nodes["DESTINATION"])
            target_node_type = "DESTINATION"
    elif inventory_operations:
        # Mixed daily plans retain the outbound destination globally for the
        # legacy outbound planner, while each inbound operation carries its
        # own storage destination.
        source_node_ids = list(labelled_nodes["INBOUND"])
        if labelled_nodes["OUTBOUND"]:
            target_node_ids = list(labelled_nodes["OUTBOUND"])
            target_node_type = "OUTBOUND"
        elif labelled_nodes["DESTINATION"]:
            target_node_ids = list(labelled_nodes["DESTINATION"])
            target_node_type = "DESTINATION"
    else:
        target_node_ids, target_node_type = _extract_explicit_target_nodes(normalized)

    if len(labelled_nodes["STORAGE"]) == 1:
        storage_node_id = labelled_nodes["STORAGE"][0]
        for operation in inventory_operations:
            if operation.operation_type == "INBOUND":
                operation.storage_node_id = storage_node_id

    detected_events = _hypothetical_events(normalized, robots, tasks)
    # "재고가 부족하면 가능한 수량만 부분 출고" is an outbound policy,
    # not a request to replace the command with a generic shortage scenario.
    # Keep real hypothetical shortage commands (for example "재고 부족을
    # 가정") unchanged, while allowing the explicit item/quantity operation
    # to continue through normal inventory projection.
    conditional_partial_shortage = (
        bool(inventory_operations)
        and any(row.allow_partial_fulfillment for row in inventory_operations)
        and bool(re.search(r"재고(?:가|이)?\s*부족하면", normalized))
    )
    # A conditional shortage handling rule attached to explicit operations is
    # also a planning policy, not a hypothetical inventory mutation.  For
    # example, "A 재고가 부족하면 A 작업만 제외하고 B는 계속 진행"
    # means that the real SQL inventory precheck should decide which operation
    # is blocked while independent operations continue.  Treating this as a
    # generic HYPOTHETICAL_SCENARIO bypasses the normal mixed-operation flow.
    conditional_independent_shortage = (
        bool(inventory_operations)
        and bool(re.search(r"재고(?:가|이)?\s*부족하면", normalized))
        and _contains(
            normalized,
            (
                "작업만 제외",
                "해당 작업만 제외",
                "나머지 작업은 계속",
                "다른 작업은 계속",
                "계속 진행",
            ),
        )
    )
    if conditional_partial_shortage or conditional_independent_shortage:
        detected_events = [
            event
            for event in detected_events
            if event.event_type != "INVENTORY_SHORTAGE"
        ]
    scenario_marker = _contains(normalized, ("가정", "시뮬레이션", "가상"))
    ambiguous_event_statement = _contains(
        normalized,
        ("고장 났", "막혔", "사용 불가"),
    )
    hypothetical = detected_events if scenario_marker or ambiguous_event_statement else []

    excluded_robots = robots if _contains(normalized, ("제외", "빼줘", "빼고")) else []
    included_robots = robots if _contains(normalized, ("다시 포함", "포함시켜")) else []
    if included_robots:
        excluded_robots = []
    robot_limit_match = re.search(r"(?:로봇(?:은|을)?\s*)?(\d+)\s*대(?:로|만|까지)?", normalized)
    robot_limit = int(robot_limit_match.group(1)) if robot_limit_match else None
    excluded_nodes = _extract_excluded_node_ids(normalized)
    edge_pairs = _extract_edge_pairs(normalized)
    excluded_edges = [f"{a}->{b}" for a, b in edge_pairs]
    closed_edges = [ClosedEdgeAssumption(from_node=a, to_node=b) for a, b in edge_pairs]

    fixed_assignments: list[FixedRobotAssignment] = []
    explicit_robot_workflow = bool(
        len(robots) == 1
        and inventory_operations
        and re.search(
            rf"{re.escape(robots[0])}(?:은|는|이|가|을|를)?"
            rf"[^.。!?]{{0,180}}(?:보내|충전(?:한|한\s*뒤|하고\s*나서)|"
            rf"(?:출고|입고)\s*작업(?:을|를)?\s*(?:수행|처리))"
            rf"[^.。!?]{{0,120}}(?:출고|입고|작업).*(?:수행|처리|이동)",
            normalized,
            re.IGNORECASE,
        )
    )
    assignment_requested = bool(robots) and (
        _contains(normalized, ("고정", "배정", "담당"))
        or explicit_robot_workflow
    )
    if assignment_requested and len(robots) == 1:
        if tasks:
            fixed_assignments.extend(
                FixedRobotAssignment(task_id=task_id, robot_id=robots[0])
                for task_id in tasks
            )
        elif inventory_operations:
            # A natural-language outbound/inbound command creates its work ID
            # from the operation ID. Pin that work ID so every generated task
            # (for example PICK and DROP) stays on the requested robot.
            fixed_assignments.extend(
                FixedRobotAssignment(
                    task_id=operation.operation_id,
                    robot_id=robots[0],
                )
                for operation in inventory_operations
            )

    hard_constraints: list[str] = []
    if _contains(normalized, ("배정 유지", "기존 배정 유지")):
        hard_constraints.append("PRESERVE_ASSIGNMENTS")
    if _contains(normalized, ("실행 중 작업 보호", "진행 중 작업 유지")):
        hard_constraints.append("PROTECT_EXECUTING_TASKS")
    if _contains(normalized, ("배터리 임계치 이하 로봇 제외", "배터리 부족 로봇 제외")):
        hard_constraints.append("EXCLUDE_LOW_BATTERY_ROBOTS")
    no_blocking_idle_requested = bool(
        schedule.daily_schedule_requested
        or _contains(
            normalized,
            (
                "길을 막지",
                "길막",
                "이동을 방해하지",
                "장시간 대기하지",
                "통로에서 대기하지",
                "교차로에서 대기하지",
                "작업 노드에서 대기하지",
                "안전한 holding",
                "안전한 홀딩",
                "주차 노드",
                "대기 노드",
            ),
        )
    )
    if no_blocking_idle_requested:
        hard_constraints.extend(
            [
                "NO_IDLE_ON_TRANSIT_NODE",
                "NO_IDLE_ON_INTERSECTION",
                "NO_IDLE_ON_SERVICE_NODE",
                "NO_IDLE_ON_ARTICULATION_NODE",
                "NO_IDLE_ON_CONGESTION_NODE",
                "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE",
                "IDLE_ONLY_ON_WHITELISTED_NODE",
            ]
        )
    charger_idle_requested = bool(
        schedule.daily_schedule_requested
        or _contains(
            normalized,
            (
                "일이 없으면 충전소",
                "일 없으면 충전소",
                "충전소로 복귀",
                "충전소에 복귀",
                "충전소에서 대기",
                "충전하며 대기",
                "기회 충전",
                "공백에 충전",
                "점심시간에 충전",
                "충전소 주변",
            ),
        )
    )
    if charger_idle_requested:
        hard_constraints.extend(
            [
                "NO_IDLE_ON_TRANSIT_NODE",
                "NO_IDLE_ON_INTERSECTION",
                "NO_IDLE_ON_SERVICE_NODE",
                "NO_IDLE_ON_ARTICULATION_NODE",
                "NO_IDLE_ON_CONGESTION_NODE",
                "NO_IDLE_ON_CHARGER_SLOT_AFTER_CHARGE",
                "IDLE_ONLY_ON_WHITELISTED_NODE",
                "LONG_IDLE_RETURN_TO_CHARGER_AREA",
                "OPPORTUNITY_CHARGING",
                "CHARGER_SLOT_ONLY_WHILE_CHARGING",
            ]
        )
    if (
        _contains(
            normalized,
            (
                "필요한 만큼 충전",
                "필요한 만큼만 충전",
                "필요한 양만 충전",
                "최소 배터리 유지",
                "최저 배터리 유지",
                "최소 기준 유지",
                "최저 기준 유지",
                "최소 배터리 기준 이상",
                "최소 배터리 기준을 이상",
                "안전 여유",
                "안전하게 도달",
                "80%까지 충전",
                "80 %까지 충전",
                "작업 투입 기준",
            ),
        )
        or re.search(r"최소(?:한의)?\s*배터리(?:를|가)?\s*유지", normalized)
        or re.search(
            r"최소\s*운용\s*배터리(?:\s*\d+(?:\.\d+)?\s*%)?(?:를|가)?\s*유지",
            normalized,
        )
    ):
        hard_constraints.append("MINIMUM_REQUIRED_CHARGE")
        hard_constraints.append("MINIMUM_BATTERY_AT_ALL_TIMES")
        if _contains(normalized, ("80%까지 충전", "80 %까지 충전", "작업 투입 기준")):
            hard_constraints.append("CHARGE_TARGET_80_PERCENT")
        if _contains(normalized, ("안전 여유", "안전하게 도달", "도달할 수 있는")):
            hard_constraints.append("SAFE_CHARGER_REACHABILITY")
    if tasks and (
        len(tasks) > 1
        or _contains(
            normalized,
            (
                "다른 작업은 포함하지 마",
                "다른 작업 포함하지 마",
                "다른 작업은 제외",
                "지정 작업만",
                "명시한 작업만",
                "작업 하나만",
                "작업만",
            ),
        )
    ):
        hard_constraints.append("EXPLICIT_TASK_SCOPE_ONLY")

    hard_constraints = list(dict.fromkeys(hard_constraints))

    missing: list[str] = [
        *schedule.missing_information,
        *inventory_missing,
        *planning_reference_errors,
    ]
    if dependency_errors:
        missing.append("cyclic_task_dependency")
    if schedule.preemption_policy == "REQUIRE_SAFE_STOP_CONFIRMATION":
        missing.append("safe_stop_confirmation")
    ambiguous = list(
        dict.fromkeys(
            [
                *ambiguous_modes,
                *ambiguous_objectives,
                *dependency_errors,
                *inventory_ambiguous,
            ]
        )
    )
    vague_targets = [
        phrase
        for phrase in ("그 로봇", "아까 작업", "그 작업", "저 계획", "이 계획", "문제 있는 작업")
        if phrase in normalized
    ]
    ambiguous.extend(vague_targets)

    if comparison:
        dimensions = []
        if robot_limit is not None or len(re.findall(r"\d+\s*대", normalized)) >= 2:
            dimensions.append("ROBOT_COUNT")
        if _contains(normalized, ("거리", "최단")):
            dimensions.append("TOTAL_DISTANCE")
        if _contains(normalized, ("시간", "빨리", "완료")):
            dimensions.append("MAKESPAN")
        if "에너지" in normalized:
            dimensions.append("ENERGY")
        if len(robots) >= 2:
            dimensions.append("ROBOT")
        if _contains(normalized, ("이전 계획", "현재 계획")):
            dimensions.append("PLAN_VERSION")
        if not dimensions:
            missing.append("comparison_dimensions")
        return CommandInterpretation(
            command_kind="QUERY",
            intent="SCENARIO_COMPARISON",
            objective=raw,
            execution_mode="SIMULATE_ONLY",
            planning_reference=planning_reference,
            target_robot_ids=robots,
            target_task_ids=tasks,
            target_plan_versions=plan_versions,
            target_simulation_ids=simulations,
            extracted_robot_ids=robots,
            extracted_task_ids=tasks,
            comparison_requested=True,
            comparison_dimensions=list(dict.fromkeys(dimensions)),
            requires_future_feature=False,
            missing_information=missing,
            ambiguous_terms=ambiguous,
            confidence=0.95 if not missing else 0.6,
            summary="What-if 시나리오 비교 요청",
        )

    if query:
        target, intent, action, query_filters = query
        inventory_quantity_filter = (
            extract_inventory_quantity_filter(raw)
            if target == "INVENTORY"
            else None
        )
        if inventory_quantity_filter is not None:
            query_filters = [*query_filters, inventory_quantity_filter]
        storage_requested = (
            target == "INVENTORY" and inventory_query_requests_storage(normalized)
        )
        return CommandInterpretation(
            command_kind="QUERY",
            intent=intent,
            objective=raw,
            query_target=target,
            query_action=action,
            target_robot_ids=robots,
            target_task_ids=tasks,
            target_simulation_ids=simulations,
            target_plan_versions=plan_versions,
            item_ids=labeled_item_ids,
            extracted_robot_ids=robots,
            extracted_task_ids=tasks,
            excluded_node_ids=excluded_nodes,
            excluded_edge_ids=excluded_edges,
            assumed_closed_node_ids=excluded_nodes,
            assumed_closed_edges=closed_edges,
            hypothetical_events=hypothetical,
            query_filters=query_filters,
            load_open_inventory_orders=(
                target == "INVENTORY" and inventory_query_requests_inbound(normalized)
            ),
            target_node_type="STORAGE" if storage_requested else None,
            required_sql_reads=[
                value
                for value in ("ROBOTS" if target == "ROBOT" else "WORKS" if target == "WORK" else "INVENTORY" if target == "INVENTORY" else None,)
                if value
            ],
            required_graph_reads=(
                ["STORAGE_NODES"]
                if storage_requested
                else ["TOPOLOGY"]
                if target in {"MAP", "EVIDENCE"}
                else []
            ),
            execution_mode="PLAN_ONLY",
            planning_reference=planning_reference,
            missing_information=missing,
            ambiguous_terms=ambiguous,
            confidence=0.98,
            summary=f"{target} {action} 조회",
        )

    urgent_insert_event = any(
        event.event_type == "URGENT_ORDER" for event in hypothetical
    ) and _contains(normalized, ("삽입", "추가")) and "가정" not in normalized
    if hypothetical and not urgent_insert_event:
        explicit_hypothesis = _contains(normalized, ("가정", "시뮬레이션", "가상"))
        explicit_actual = _contains(normalized, ("실제 반영", "운영에 반영"))
        if not explicit_hypothesis and not explicit_actual and _contains(normalized, ("고장 났", "막혔", "사용 불가")):
            ambiguous.append("hypothetical_or_real")
            missing.append("event_application_mode")
        return CommandInterpretation(
            command_kind="PLAN",
            intent="HYPOTHETICAL_SCENARIO",
            objective=raw,
            inventory_operations=inventory_operations,
            load_open_inventory_orders=load_open_inventory_orders,
            item_ids=sorted({row.item_id for row in inventory_operations}),
            quantity=(
                inventory_operations[0].quantity_boxes
                if len(inventory_operations) == 1
                else None
            ),
            execution_mode="SIMULATE_ONLY",
            planning_reference=planning_reference,
            source_node_ids=source_node_ids,
            target_node_ids=target_node_ids,
            target_node_type=target_node_type,
            target_robot_ids=robots,
            target_task_ids=tasks,
            target_plan_versions=plan_versions,
            extracted_robot_ids=robots,
            extracted_task_ids=tasks,
            excluded_robot_ids=excluded_robots,
            included_robot_ids=included_robots,
            excluded_node_ids=excluded_nodes,
            excluded_edge_ids=excluded_edges,
            assumed_closed_node_ids=excluded_nodes,
            assumed_closed_edges=closed_edges,
            hypothetical_events=hypothetical,
            fixed_robot_assignments=fixed_assignments,
            scheduled_task_constraints=schedule.constraints,
            task_dependencies=schedule.dependencies,
            insertion_policy=schedule.insertion_policy,
            preemption_policy=schedule.preemption_policy,
            same_robot_groups=schedule.same_robot_groups,
            daily_schedule_requested=(
                schedule.daily_schedule_requested
                or planning_reference is not None
            ),
            robot_limit=robot_limit,
            optimization_priority=optimization_priority,
            optimization_weights=weights,
            hard_constraints=hard_constraints,
            missing_information=missing,
            ambiguous_terms=list(dict.fromkeys(ambiguous)),
            confidence=0.9 if not missing else 0.6,
            summary="가상 운영 시나리오",
        )

    if _contains(normalized, ("복합 재계획", "전체 제약을 분석", "복합 최적화")):
        return CommandInterpretation(
            command_kind="PLAN",
            intent="OTHER",
            objective=raw,
            execution_mode=mode or "PLAN_ONLY",
            missing_information=["command_intent"],
            ambiguous_terms=["complex_planning_scope"],
            confidence=0.2,
            summary="복합 명령의 범위를 결정적으로 확정할 수 없음",
        )

    planning_signal = (
        schedule.daily_schedule_requested
        or bool(inventory_operations)
        or load_open_inventory_orders
        or bool(inventory_missing)
        or _contains(
        normalized,
        (
            "계획",
            "재계획",
            "배정",
            "긴급",
            "제외",
            "포함",
            "고정",
            "최소화",
            "우선",
            "실행",
            "시뮬레이션",
        ),
        )
        or bool(
        mode
        or robot_limit
        or fixed_assignments
        or included_robots
        or optimization_priority
        )
    )

    if planning_signal:
        operation_types = {row.operation_type for row in inventory_operations}
        if "전체" in normalized and "재계획" in normalized:
            intent = "GLOBAL_REPLAN"
        elif "재계획" in normalized:
            intent = "LOCAL_REPLAN"
        elif (
            schedule.insertion_policy == "URGENT"
            or "긴급" in normalized
            or _contains(normalized, ("삽입", "추가"))
        ):
            intent = "INSERT_TASK"
        elif mode == "EXECUTE":
            intent = "EXECUTE"
        elif len(operation_types) == 1 and not load_open_inventory_orders:
            intent = next(iter(operation_types))
        else:
            intent = "DAILY_PLAN"
        effective_mode = mode or "PLAN_ONLY"
        if ambiguous_modes:
            missing.append("requested_execution_mode")
        if ambiguous_objectives:
            missing.append("optimization_priority")
        if vague_targets:
            missing.append("target_reference")
        return CommandInterpretation(
            command_kind="EXECUTE" if effective_mode == "EXECUTE" else "PLAN",
            intent=intent,
            objective=raw,
            inventory_operations=inventory_operations,
            load_open_inventory_orders=load_open_inventory_orders,
            item_ids=sorted({row.item_id for row in inventory_operations}),
            quantity=(
                inventory_operations[0].quantity_boxes
                if len(inventory_operations) == 1
                else None
            ),
            execution_mode=effective_mode,
            planning_reference=planning_reference,
            source_node_ids=source_node_ids,
            target_node_ids=target_node_ids,
            target_node_type=target_node_type,
            target_robot_ids=robots,
            target_task_ids=tasks,
            target_plan_versions=plan_versions,
            extracted_robot_ids=robots,
            extracted_task_ids=tasks,
            excluded_robot_ids=excluded_robots,
            included_robot_ids=included_robots,
            excluded_node_ids=excluded_nodes,
            excluded_edge_ids=excluded_edges,
            assumed_closed_node_ids=excluded_nodes,
            assumed_closed_edges=closed_edges,
            fixed_robot_assignments=fixed_assignments,
            scheduled_task_constraints=schedule.constraints,
            task_dependencies=schedule.dependencies,
            insertion_policy=schedule.insertion_policy,
            preemption_policy=schedule.preemption_policy,
            same_robot_groups=schedule.same_robot_groups,
            daily_schedule_requested=(
                schedule.daily_schedule_requested
                or planning_reference is not None
            ),
            robot_limit=robot_limit,
            optimization_priority=optimization_priority,
            optimization_weights=weights,
            hard_constraints=hard_constraints,
            priority=(
                "EMERGENCY"
                if schedule.insertion_policy == "URGENT" or "긴급" in normalized
                else "NORMAL"
            ),
            missing_information=list(dict.fromkeys(missing)),
            ambiguous_terms=list(dict.fromkeys(ambiguous)),
            confidence=0.95 if not missing else 0.6,
            summary="결정적 규칙으로 해석한 계획 명령",
        )

    unresolved = ["command_intent"]
    if ambiguous_modes:
        unresolved.append("requested_execution_mode")
    if ambiguous_objectives:
        unresolved.append("optimization_priority")
    if vague_targets:
        unresolved.append("target_reference")
    return CommandInterpretation(
        command_kind="PLAN",
        intent="OTHER",
        objective=raw,
        execution_mode=mode or "PLAN_ONLY",
        target_robot_ids=robots,
        target_task_ids=tasks,
        extracted_robot_ids=robots,
        extracted_task_ids=tasks,
        missing_information=unresolved,
        ambiguous_terms=list(dict.fromkeys(ambiguous)),
        confidence=0.2,
        summary="명령 의도를 안전하게 확정할 수 없음",
    )


def is_deterministically_supported(interpretation: CommandInterpretation) -> bool:
    return interpretation.intent != "OTHER" and interpretation.confidence >= 0.6


def requires_deterministic_clarification(
    interpretation: CommandInterpretation,
) -> bool:
    clarification_fields = {
        "requested_execution_mode",
        "optimization_priority",
        "target_reference",
        "event_application_mode",
        "comparison_dimensions",
    }
    return bool(clarification_fields.intersection(interpretation.missing_information))
