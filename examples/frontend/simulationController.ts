/**
 * Minimal LARO v13.20 browser playback controller.
 *
 * Backend times remain absolute simulation milliseconds.  The browser advances
 * logic on a fixed tick and interpolates MOVE steps every animation frame.
 */
export type StepType = "MOVE" | "WAIT" | "SERVICE";

export interface SimulationStep {
  step_id: string;
  sequence: number;
  step_type: StepType;
  start_at_ms: number;
  end_at_ms: number;
  node_id?: string | null;
  edge_id?: string | null;
  from_node?: string | null;
  to_node?: string | null;
  task_id?: string | null;
  service_kind?: string | null;
  reason?: string | null;
}

export interface RobotPlan {
  robot_id: string;
  initial_node: string;
  available_at_ms: number;
  finish_at_ms: number;
  steps: SimulationStep[];
}

export interface HandoverPoint {
  robot_id: string;
  node_id: string;
  handover_at_ms: number;
  handover_policy:
    | "CURRENT_NODE"
    | "NEXT_NODE"
    | "CURRENT_SERVICE_END"
    | "CURRENT_OPERATION_END";
}

export interface SimulationPlan {
  plan_id: string;
  plan_version: number;
  base_plan_id?: string | null;
  plan_start_sim_time_ms: number;
  absolute_finish_at_ms: number;
  sim_tick_ms: number;
  robots: RobotPlan[];
  handover_points: HandoverPoint[];
}

export interface MapNode {
  id: string;
  x: number;
  y: number;
}

export type NodeMap = Record<string, MapNode>;

export function findStep(robot: RobotPlan, timeMs: number): SimulationStep | null {
  return (
    robot.steps.find(
      (step) => step.start_at_ms <= timeMs && timeMs < step.end_at_ms,
    ) ?? null
  );
}

export function robotPosition(
  robot: RobotPlan,
  timeMs: number,
  nodes: NodeMap,
): { x: number; y: number; status: string } {
  const step = findStep(robot, timeMs);
  if (!step) {
    const last = [...robot.steps]
      .reverse()
      .find((value) => value.end_at_ms <= timeMs);
    const nodeId = last?.to_node ?? last?.node_id ?? robot.initial_node;
    const node = nodes[nodeId];
    return { x: node.x, y: node.y, status: "IDLE" };
  }

  if (step.step_type === "MOVE") {
    const from = nodes[step.from_node!];
    const to = nodes[step.to_node!];
    const progress = Math.max(
      0,
      Math.min(
        1,
        (timeMs - step.start_at_ms) /
          (step.end_at_ms - step.start_at_ms),
      ),
    );
    return {
      x: from.x + (to.x - from.x) * progress,
      y: from.y + (to.y - from.y) * progress,
      status: "MOVING",
    };
  }

  const node = nodes[step.node_id!];
  return {
    x: node.x,
    y: node.y,
    status: step.step_type === "WAIT" ? "WAITING" : step.service_kind ?? "WORKING",
  };
}

/** Select the old or new per-robot timeline at the robot's own handover. */
export function activeRobotPlan(
  robotId: string,
  timeMs: number,
  oldPlan: SimulationPlan,
  newPlan?: SimulationPlan,
): RobotPlan | null {
  if (!newPlan) {
    return oldPlan.robots.find((value) => value.robot_id === robotId) ?? null;
  }
  const handover = newPlan.handover_points.find(
    (value) => value.robot_id === robotId,
  );
  if (!handover || timeMs < handover.handover_at_ms) {
    return oldPlan.robots.find((value) => value.robot_id === robotId) ?? null;
  }
  return (
    newPlan.robots.find((value) => value.robot_id === robotId) ?? {
      robot_id: robotId,
      initial_node: handover.node_id,
      available_at_ms: handover.handover_at_ms,
      finish_at_ms: handover.handover_at_ms,
      steps: [],
    }
  );
}

export class SimulationClock {
  private accumulatorMs = 0;
  private lastRealMs = performance.now();
  public simTimeMs: number;
  public playbackSpeed = 1;
  public running = false;

  constructor(
    private readonly plan: SimulationPlan,
    private readonly onTick: (fromMs: number, toMs: number) => void,
    private readonly onRender: (timeMs: number) => void,
  ) {
    this.simTimeMs = plan.plan_start_sim_time_ms;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.lastRealMs = performance.now();
    requestAnimationFrame(this.loop);
  }

  pause(): void {
    this.running = false;
  }

  private loop = (realNow: number): void => {
    if (!this.running) return;
    const realDelta = Math.min(realNow - this.lastRealMs, 250);
    this.lastRealMs = realNow;
    this.accumulatorMs += realDelta * this.playbackSpeed;

    while (
      this.accumulatorMs >= this.plan.sim_tick_ms &&
      this.simTimeMs < this.plan.absolute_finish_at_ms
    ) {
      const next = Math.min(
        this.simTimeMs + this.plan.sim_tick_ms,
        this.plan.absolute_finish_at_ms,
      );
      this.onTick(this.simTimeMs, next);
      this.simTimeMs = next;
      this.accumulatorMs -= this.plan.sim_tick_ms;
    }

    this.onRender(
      Math.min(
        this.simTimeMs + this.accumulatorMs,
        this.plan.absolute_finish_at_ms,
      ),
    );
    if (this.simTimeMs < this.plan.absolute_finish_at_ms) {
      requestAnimationFrame(this.loop);
    } else {
      this.running = false;
    }
  };
}
