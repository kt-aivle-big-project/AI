--
-- PostgreSQL database dump
--

\restrict xIITSX0ZiavH3hsNlooskHA2qyOhy0IpGbDmAMIdfc0aKwBQgbtkpH0f5dKoaAH

-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: automatic_replan_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.automatic_replan_request (
    request_id text NOT NULL,
    event_id text NOT NULL,
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    scope text NOT NULL,
    status text NOT NULL,
    execution_context text NOT NULL,
    affected_robot_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    affected_task_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    expected_active_plan_version text,
    generated_plan_version text,
    simulation_id text,
    verification_decision text,
    approval_required boolean DEFAULT false NOT NULL,
    result_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    approved_by text,
    approval_reason text,
    approved_at timestamp with time zone,
    rejected_by text,
    rejection_reason text,
    rejected_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: clarification_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clarification_request (
    clarification_id text NOT NULL,
    conversation_id text,
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    status text NOT NULL,
    reason_code text NOT NULL,
    question text NOT NULL,
    missing_fields jsonb DEFAULT '[]'::jsonb NOT NULL,
    ambiguous_fields jsonb DEFAULT '[]'::jsonb NOT NULL,
    options jsonb DEFAULT '[]'::jsonb NOT NULL,
    original_text text NOT NULL,
    response jsonb,
    resolved_command_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    resolved_at timestamp with time zone
);


--
-- Name: command_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.command_history (
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    command_type character varying(30),
    requested_execution_mode character varying(30),
    resolved_execution_mode character varying(30),
    source character varying(30),
    original_text text,
    actor_id text,
    status character varying(40) NOT NULL,
    simulation_id text,
    plan_version text,
    parent_command_id text,
    received_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    result_summary jsonb,
    error_summary jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: conversation_command_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_command_link (
    conversation_id text NOT NULL,
    command_id text NOT NULL,
    parent_command_id text,
    sequence_number integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: conversation_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_session (
    conversation_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    status text DEFAULT 'ACTIVE'::text NOT NULL,
    active_command_id text,
    active_plan_version text,
    active_simulation_id text,
    active_clarification_id text,
    resolved_constraints jsonb DEFAULT '{}'::jsonb NOT NULL,
    summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: execution_event_processing; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.execution_event_processing (
    event_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    event_type text NOT NULL,
    event_source text NOT NULL,
    status text NOT NULL,
    event_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    impact_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    failure_signature text,
    generated_command_id text,
    generated_plan_version text,
    replan_request_id text,
    approval_required boolean DEFAULT false NOT NULL,
    result_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone
);


--
-- Name: execution_plan_approval; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.execution_plan_approval (
    plan_version text NOT NULL,
    warehouse_id bigint NOT NULL,
    command_id text,
    verification_decision text NOT NULL,
    status text NOT NULL,
    plan_fingerprint text NOT NULL,
    expected_active_plan_version text,
    approved_by text NOT NULL,
    approval_reason text NOT NULL,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_by text,
    revocation_reason text,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_execution_plan_approval_status CHECK ((status = ANY (ARRAY['APPROVED'::text, 'REVOKED'::text])))
);


--
-- Name: inbound_order_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbound_order_line (
    inbound_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    item_id text NOT NULL,
    quantity_boxes integer NOT NULL,
    expected_arrival_at timestamp with time zone,
    expected_available_at timestamp with time zone,
    actual_arrival_at timestamp with time zone,
    actual_available_at timestamp with time zone,
    status text DEFAULT 'SCHEDULED'::text NOT NULL,
    storage_node_id bigint,
    lot_id text,
    warehouse_item_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_inbound_quantity_boxes CHECK ((quantity_boxes > 0))
);


--
-- Name: inventory_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_item (
    item_id text NOT NULL,
    item_name text NOT NULL,
    base_unit text DEFAULT 'BOX'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_inventory_item_base_unit CHECK ((base_unit = 'BOX'::text))
);


--
-- Name: inventory_movement; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_movement (
    movement_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    item_id text NOT NULL,
    lot_id text,
    warehouse_item_id text,
    work_id text,
    order_id text,
    plan_version text,
    movement_type text NOT NULL,
    quantity_delta_boxes integer NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: inventory_reservation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_reservation (
    reservation_id character varying(50) NOT NULL,
    warehouse_item_id character varying(50) NOT NULL,
    work_id character varying(50),
    quantity integer NOT NULL,
    status character varying(30) DEFAULT 'ACTIVE'::character varying NOT NULL,
    reserved_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    version bigint DEFAULT 0 NOT NULL,
    CONSTRAINT inventory_reservation_quantity_check CHECK ((quantity > 0))
);


--
-- Name: outbound_order_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbound_order_line (
    outbound_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    item_id text NOT NULL,
    requested_quantity_boxes integer NOT NULL,
    required_by timestamp with time zone,
    priority text DEFAULT 'NORMAL'::text NOT NULL,
    allow_partial_fulfillment boolean DEFAULT false NOT NULL,
    status text DEFAULT 'OPEN'::text NOT NULL,
    work_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_outbound_quantity_boxes CHECK ((requested_quantity_boxes > 0))
);


--
-- Name: outbox_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbox_event (
    outbox_id character varying(50) NOT NULL,
    aggregate_type character varying(50) NOT NULL,
    aggregate_id character varying(50) NOT NULL,
    event_type character varying(100) NOT NULL,
    payload jsonb NOT NULL,
    status character varying(30) DEFAULT 'PENDING'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone
);


--
-- Name: planning_stage_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.planning_stage_log (
    stage_log_id bigint NOT NULL,
    command_id text NOT NULL,
    sequence integer NOT NULL,
    node_name character varying(80) NOT NULL,
    attempt integer DEFAULT 1 NOT NULL,
    status character varying(30) NOT NULL,
    message text,
    details jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: planning_stage_log_stage_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.planning_stage_log_stage_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: planning_stage_log_stage_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.planning_stage_log_stage_log_id_seq OWNED BY public.planning_stage_log.stage_log_id;


--
-- Name: robot; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.robot (
    robot_id character varying(50) NOT NULL,
    warehouse_id bigint NOT NULL,
    robot_code character varying(50) NOT NULL,
    node_id bigint NOT NULL,
    battery numeric(5,2) NOT NULL,
    status character varying(30) NOT NULL,
    max_load numeric(12,2) NOT NULL,
    current_load numeric(12,2) DEFAULT 0 NOT NULL,
    version bigint DEFAULT 0 NOT NULL,
    CONSTRAINT robot_battery_check CHECK (((battery >= (0)::numeric) AND (battery <= (100)::numeric))),
    CONSTRAINT robot_check CHECK ((current_load <= max_load)),
    CONSTRAINT robot_current_load_check CHECK ((current_load >= (0)::numeric)),
    CONSTRAINT robot_max_load_check CHECK ((max_load >= (0)::numeric))
);


--
-- Name: robot_execution_dispatch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.robot_execution_dispatch (
    dispatch_id text NOT NULL,
    idempotency_key text NOT NULL,
    warehouse_id bigint NOT NULL,
    command_id text,
    plan_version text NOT NULL,
    approved_plan_fingerprint text NOT NULL,
    payload_fingerprint text NOT NULL,
    previous_active_plan_version text,
    status text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    command_batches jsonb DEFAULT '[]'::jsonb NOT NULL,
    command_states jsonb DEFAULT '[]'::jsonb NOT NULL,
    gateway_result jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    dispatched_at timestamp with time zone,
    completed_at timestamp with time zone,
    CONSTRAINT ck_robot_execution_dispatch_status CHECK ((status = ANY (ARRAY['PREPARED'::text, 'DISPATCHING'::text, 'AWAITING_ACK'::text, 'PARTIAL_ACK'::text, 'COMPLETED'::text, 'DISPATCH_TIMEOUT'::text, 'RETRY_EXHAUSTED'::text, 'PARTIAL_FAILURE'::text, 'CANCELED'::text, 'CANCELED_PARTIAL_EXECUTION'::text, 'ROLLED_BACK'::text]))),
    CONSTRAINT robot_execution_dispatch_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT robot_execution_dispatch_max_attempts_check CHECK ((max_attempts >= 1))
);


--
-- Name: scenario_comparison; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scenario_comparison (
    comparison_id text NOT NULL,
    request_key text NOT NULL,
    conversation_id text,
    warehouse_id bigint NOT NULL,
    command_id text NOT NULL,
    status text NOT NULL,
    request_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    recommendation_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: scenario_comparison_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scenario_comparison_run (
    comparison_id text NOT NULL,
    scenario_id text NOT NULL,
    simulation_id text,
    command_id text NOT NULL,
    status text NOT NULL,
    scenario_definition jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: simulation_reset_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.simulation_reset_audit (
    reset_id text NOT NULL,
    command_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    target_type character varying(30) NOT NULL,
    target_simulation_id text,
    actor_id text,
    reason text NOT NULL,
    status character varying(30) NOT NULL,
    affected_simulation_count integer DEFAULT 0 NOT NULL,
    before_summary jsonb,
    after_summary jsonb,
    failure_summary jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);


--
-- Name: simulation_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.simulation_run (
    run_id character varying(50) NOT NULL,
    command_id character varying(50) NOT NULL,
    warehouse_id bigint NOT NULL,
    plan_version character varying(100),
    status character varying(50) NOT NULL,
    input_payload jsonb NOT NULL,
    output_payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    simulation_id text,
    current_state jsonb,
    checkpoint text
);


--
-- Name: simulation_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.simulation_session (
    simulation_id text NOT NULL,
    warehouse_id bigint NOT NULL,
    status character varying(30) NOT NULL,
    generation integer DEFAULT 1 NOT NULL,
    base_state jsonb NOT NULL,
    current_state jsonb NOT NULL,
    checkpoint text,
    created_by_command_id text,
    last_command_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reset_at timestamp with time zone,
    reset_by text,
    reset_reason text
);


--
-- Name: warehouse_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warehouse_items (
    warehouse_item_id character varying(50) NOT NULL,
    warehouse_id bigint NOT NULL,
    item_id character varying(100) NOT NULL,
    lot_id character varying(100),
    node_id bigint NOT NULL,
    quantity integer NOT NULL,
    reserved_quantity integer DEFAULT 0 NOT NULL,
    expiry_date date,
    version bigint DEFAULT 0 NOT NULL,
    status text DEFAULT 'AVAILABLE'::text NOT NULL,
    received_at timestamp with time zone,
    available_at timestamp with time zone,
    expiration_at timestamp with time zone,
    base_unit text DEFAULT 'BOX'::text NOT NULL,
    CONSTRAINT warehouse_items_check CHECK ((reserved_quantity <= quantity)),
    CONSTRAINT warehouse_items_quantity_check CHECK ((quantity >= 0)),
    CONSTRAINT warehouse_items_reserved_quantity_check CHECK ((reserved_quantity >= 0))
);


--
-- Name: work_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_dependencies (
    predecessor_work_id text NOT NULL,
    successor_work_id text NOT NULL,
    dependency_type text DEFAULT 'FINISH_TO_START'::text NOT NULL,
    lag_seconds integer DEFAULT 0 NOT NULL,
    source_command_id text,
    plan_version text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_work_dependency_not_self CHECK ((predecessor_work_id <> successor_work_id)),
    CONSTRAINT work_dependencies_lag_seconds_check CHECK ((lag_seconds >= 0))
);


--
-- Name: work_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_event (
    event_id character varying(50) NOT NULL,
    work_id character varying(50),
    robot_id character varying(50),
    event_type character varying(50) NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone NOT NULL
);


--
-- Name: work_schedule_constraints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_schedule_constraints (
    work_id text NOT NULL,
    earliest_start timestamp with time zone,
    latest_finish timestamp with time zone,
    time_constraint_type text DEFAULT 'ASAP'::text NOT NULL,
    fixed_robot_id text,
    same_robot_group text,
    sequence_group text,
    sequence_order integer,
    source_command_id text,
    plan_version text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_work_schedule_window CHECK (((earliest_start IS NULL) OR (latest_finish IS NULL) OR (earliest_start <= latest_finish))),
    CONSTRAINT work_schedule_constraints_sequence_order_check CHECK (((sequence_order IS NULL) OR (sequence_order > 0)))
);


--
-- Name: works; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.works (
    work_id character varying(50) NOT NULL,
    warehouse_id bigint NOT NULL,
    task_code character varying(30) NOT NULL,
    item_id character varying(100),
    quantity integer DEFAULT 0 NOT NULL,
    source_node bigint,
    target_node bigint,
    priority smallint DEFAULT 50 NOT NULL,
    status character varying(30) NOT NULL,
    assigned_robot_id character varying(50),
    scheduled_start timestamp with time zone,
    scheduled_end timestamp with time zone,
    version bigint DEFAULT 0 NOT NULL,
    actual_started_at timestamp with time zone,
    actual_completed_at timestamp with time zone,
    operation_type text,
    quantity_boxes integer,
    required_at timestamp with time zone,
    allow_partial_fulfillment boolean DEFAULT false NOT NULL,
    inventory_order_id text,
    CONSTRAINT works_priority_check CHECK (((priority >= 1) AND (priority <= 100))),
    CONSTRAINT works_quantity_check CHECK ((quantity >= 0))
);


--
-- Name: planning_stage_log stage_log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planning_stage_log ALTER COLUMN stage_log_id SET DEFAULT nextval('public.planning_stage_log_stage_log_id_seq'::regclass);


--
-- Name: automatic_replan_request automatic_replan_request_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automatic_replan_request
    ADD CONSTRAINT automatic_replan_request_event_id_key UNIQUE (event_id);


--
-- Name: automatic_replan_request automatic_replan_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automatic_replan_request
    ADD CONSTRAINT automatic_replan_request_pkey PRIMARY KEY (request_id);


--
-- Name: clarification_request clarification_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clarification_request
    ADD CONSTRAINT clarification_request_pkey PRIMARY KEY (clarification_id);


--
-- Name: command_history command_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.command_history
    ADD CONSTRAINT command_history_pkey PRIMARY KEY (command_id);


--
-- Name: conversation_command_link conversation_command_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_command_link
    ADD CONSTRAINT conversation_command_link_pkey PRIMARY KEY (conversation_id, command_id);


--
-- Name: conversation_session conversation_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_session
    ADD CONSTRAINT conversation_session_pkey PRIMARY KEY (conversation_id);


--
-- Name: execution_event_processing execution_event_processing_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_event_processing
    ADD CONSTRAINT execution_event_processing_pkey PRIMARY KEY (event_id);


--
-- Name: execution_plan_approval execution_plan_approval_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_plan_approval
    ADD CONSTRAINT execution_plan_approval_pkey PRIMARY KEY (plan_version);


--
-- Name: inbound_order_line inbound_order_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbound_order_line
    ADD CONSTRAINT inbound_order_line_pkey PRIMARY KEY (inbound_id);


--
-- Name: inventory_item inventory_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_item
    ADD CONSTRAINT inventory_item_pkey PRIMARY KEY (item_id);


--
-- Name: inventory_movement inventory_movement_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_movement
    ADD CONSTRAINT inventory_movement_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: inventory_movement inventory_movement_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_movement
    ADD CONSTRAINT inventory_movement_pkey PRIMARY KEY (movement_id);


--
-- Name: inventory_reservation inventory_reservation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_reservation
    ADD CONSTRAINT inventory_reservation_pkey PRIMARY KEY (reservation_id);


--
-- Name: outbound_order_line outbound_order_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbound_order_line
    ADD CONSTRAINT outbound_order_line_pkey PRIMARY KEY (outbound_id);


--
-- Name: outbox_event outbox_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox_event
    ADD CONSTRAINT outbox_event_pkey PRIMARY KEY (outbox_id);


--
-- Name: planning_stage_log planning_stage_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planning_stage_log
    ADD CONSTRAINT planning_stage_log_pkey PRIMARY KEY (stage_log_id);


--
-- Name: robot_execution_dispatch robot_execution_dispatch_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robot_execution_dispatch
    ADD CONSTRAINT robot_execution_dispatch_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: robot_execution_dispatch robot_execution_dispatch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robot_execution_dispatch
    ADD CONSTRAINT robot_execution_dispatch_pkey PRIMARY KEY (dispatch_id);


--
-- Name: robot robot_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robot
    ADD CONSTRAINT robot_pkey PRIMARY KEY (robot_id);


--
-- Name: robot robot_warehouse_id_robot_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robot
    ADD CONSTRAINT robot_warehouse_id_robot_code_key UNIQUE (warehouse_id, robot_code);


--
-- Name: scenario_comparison scenario_comparison_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenario_comparison
    ADD CONSTRAINT scenario_comparison_pkey PRIMARY KEY (comparison_id);


--
-- Name: scenario_comparison scenario_comparison_request_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenario_comparison
    ADD CONSTRAINT scenario_comparison_request_key_key UNIQUE (request_key);


--
-- Name: scenario_comparison_run scenario_comparison_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenario_comparison_run
    ADD CONSTRAINT scenario_comparison_run_pkey PRIMARY KEY (comparison_id, scenario_id);


--
-- Name: simulation_reset_audit simulation_reset_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.simulation_reset_audit
    ADD CONSTRAINT simulation_reset_audit_pkey PRIMARY KEY (reset_id);


--
-- Name: simulation_run simulation_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.simulation_run
    ADD CONSTRAINT simulation_run_pkey PRIMARY KEY (run_id);


--
-- Name: simulation_session simulation_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.simulation_session
    ADD CONSTRAINT simulation_session_pkey PRIMARY KEY (simulation_id);


--
-- Name: planning_stage_log uq_planning_stage_command_sequence_attempt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planning_stage_log
    ADD CONSTRAINT uq_planning_stage_command_sequence_attempt UNIQUE (command_id, sequence, attempt);


--
-- Name: conversation_command_link ux_conversation_command; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_command_link
    ADD CONSTRAINT ux_conversation_command UNIQUE (command_id);


--
-- Name: warehouse_items warehouse_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse_items
    ADD CONSTRAINT warehouse_items_pkey PRIMARY KEY (warehouse_item_id);


--
-- Name: work_dependencies work_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_dependencies
    ADD CONSTRAINT work_dependencies_pkey PRIMARY KEY (predecessor_work_id, successor_work_id);


--
-- Name: work_event work_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_event
    ADD CONSTRAINT work_event_pkey PRIMARY KEY (event_id);


--
-- Name: work_schedule_constraints work_schedule_constraints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_schedule_constraints
    ADD CONSTRAINT work_schedule_constraints_pkey PRIMARY KEY (work_id);


--
-- Name: works works_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.works
    ADD CONSTRAINT works_pkey PRIMARY KEY (work_id);


--
-- Name: idx_auto_replan_command; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auto_replan_command ON public.automatic_replan_request USING btree (command_id);


--
-- Name: idx_auto_replan_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auto_replan_status ON public.automatic_replan_request USING btree (status, created_at DESC);


--
-- Name: idx_auto_replan_warehouse_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auto_replan_warehouse_created ON public.automatic_replan_request USING btree (warehouse_id, created_at DESC);


--
-- Name: idx_clarification_command; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clarification_command ON public.clarification_request USING btree (command_id);


--
-- Name: idx_clarification_conversation_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clarification_conversation_created ON public.clarification_request USING btree (conversation_id, created_at DESC);


--
-- Name: idx_clarification_status_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clarification_status_created ON public.clarification_request USING btree (status, created_at DESC);


--
-- Name: idx_command_history_actor_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_history_actor_received ON public.command_history USING btree (actor_id, received_at DESC) WHERE (actor_id IS NOT NULL);


--
-- Name: idx_command_history_plan_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_history_plan_version ON public.command_history USING btree (plan_version) WHERE (plan_version IS NOT NULL);


--
-- Name: idx_command_history_simulation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_history_simulation ON public.command_history USING btree (simulation_id) WHERE (simulation_id IS NOT NULL);


--
-- Name: idx_command_history_status_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_history_status_received ON public.command_history USING btree (status, received_at DESC);


--
-- Name: idx_command_history_warehouse_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_history_warehouse_received ON public.command_history USING btree (warehouse_id, received_at DESC);


--
-- Name: idx_conversation_link_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_link_parent ON public.conversation_command_link USING btree (parent_command_id);


--
-- Name: idx_conversation_warehouse_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_warehouse_updated ON public.conversation_session USING btree (warehouse_id, updated_at DESC);


--
-- Name: idx_execution_event_signature_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_event_signature_created ON public.execution_event_processing USING btree (warehouse_id, failure_signature, created_at DESC);


--
-- Name: idx_execution_event_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_event_status ON public.execution_event_processing USING btree (status, created_at DESC);


--
-- Name: idx_execution_event_warehouse_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_event_warehouse_created ON public.execution_event_processing USING btree (warehouse_id, created_at DESC);


--
-- Name: idx_execution_plan_approval_warehouse; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_execution_plan_approval_warehouse ON public.execution_plan_approval USING btree (warehouse_id, approved_at DESC);


--
-- Name: idx_inbound_order_item_available; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inbound_order_item_available ON public.inbound_order_line USING btree (warehouse_id, item_id, expected_available_at);


--
-- Name: idx_inbound_order_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inbound_order_open ON public.inbound_order_line USING btree (warehouse_id, status, expected_available_at);


--
-- Name: idx_inventory_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inventory_lookup ON public.warehouse_items USING btree (warehouse_id, item_id, expiry_date);


--
-- Name: idx_inventory_movement_item_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inventory_movement_item_time ON public.inventory_movement USING btree (warehouse_id, item_id, occurred_at DESC);


--
-- Name: idx_inventory_movement_work; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inventory_movement_work ON public.inventory_movement USING btree (work_id, occurred_at DESC) WHERE (work_id IS NOT NULL);


--
-- Name: idx_outbound_order_item_required; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_outbound_order_item_required ON public.outbound_order_line USING btree (warehouse_id, item_id, required_by);


--
-- Name: idx_outbound_order_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_outbound_order_open ON public.outbound_order_line USING btree (warehouse_id, status, required_by, priority);


--
-- Name: idx_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_outbox_pending ON public.outbox_event USING btree (status, created_at);


--
-- Name: idx_planning_stage_command_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_planning_stage_command_order ON public.planning_stage_log USING btree (command_id, sequence, attempt);


--
-- Name: idx_robot_execution_dispatch_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_robot_execution_dispatch_plan ON public.robot_execution_dispatch USING btree (plan_version, created_at DESC);


--
-- Name: idx_robot_execution_dispatch_warehouse_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_robot_execution_dispatch_warehouse_status ON public.robot_execution_dispatch USING btree (warehouse_id, status, updated_at DESC);


--
-- Name: idx_robot_warehouse_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_robot_warehouse_status ON public.robot USING btree (warehouse_id, status);


--
-- Name: idx_scenario_comparison_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenario_comparison_conversation ON public.scenario_comparison USING btree (conversation_id, created_at DESC);


--
-- Name: idx_scenario_comparison_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenario_comparison_status ON public.scenario_comparison USING btree (status, created_at DESC);


--
-- Name: idx_scenario_comparison_warehouse_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenario_comparison_warehouse_created ON public.scenario_comparison USING btree (warehouse_id, created_at DESC);


--
-- Name: idx_scenario_run_scenario; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenario_run_scenario ON public.scenario_comparison_run USING btree (scenario_id);


--
-- Name: idx_scenario_run_simulation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenario_run_simulation ON public.scenario_comparison_run USING btree (simulation_id);


--
-- Name: idx_simulation_reset_audit_command; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_reset_audit_command ON public.simulation_reset_audit USING btree (command_id);


--
-- Name: idx_simulation_reset_audit_target_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_reset_audit_target_created ON public.simulation_reset_audit USING btree (target_simulation_id, created_at DESC) WHERE (target_simulation_id IS NOT NULL);


--
-- Name: idx_simulation_reset_audit_warehouse_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_reset_audit_warehouse_created ON public.simulation_reset_audit USING btree (warehouse_id, created_at DESC);


--
-- Name: idx_simulation_run_command; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_run_command ON public.simulation_run USING btree (command_id);


--
-- Name: idx_simulation_run_simulation_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_run_simulation_created ON public.simulation_run USING btree (simulation_id, created_at DESC) WHERE (simulation_id IS NOT NULL);


--
-- Name: idx_simulation_run_warehouse_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_run_warehouse_created ON public.simulation_run USING btree (warehouse_id, created_at DESC);


--
-- Name: idx_simulation_session_created_command; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_session_created_command ON public.simulation_session USING btree (created_by_command_id) WHERE (created_by_command_id IS NOT NULL);


--
-- Name: idx_simulation_session_last_command; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_session_last_command ON public.simulation_session USING btree (last_command_id) WHERE (last_command_id IS NOT NULL);


--
-- Name: idx_simulation_session_warehouse_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_session_warehouse_status ON public.simulation_session USING btree (warehouse_id, status);


--
-- Name: idx_simulation_session_warehouse_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_simulation_session_warehouse_updated ON public.simulation_session USING btree (warehouse_id, updated_at DESC);


--
-- Name: idx_warehouse_items_fefo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warehouse_items_fefo ON public.warehouse_items USING btree (warehouse_id, item_id, expiration_at, available_at, lot_id);


--
-- Name: idx_warehouse_items_projection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warehouse_items_projection ON public.warehouse_items USING btree (warehouse_id, item_id, status, available_at);


--
-- Name: idx_work_dependencies_successor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_dependencies_successor ON public.work_dependencies USING btree (successor_work_id);


--
-- Name: idx_work_event_work_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_event_work_time ON public.work_event USING btree (work_id, occurred_at);


--
-- Name: idx_work_schedule_earliest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_schedule_earliest ON public.work_schedule_constraints USING btree (earliest_start);


--
-- Name: idx_work_schedule_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_schedule_latest ON public.work_schedule_constraints USING btree (latest_finish);


--
-- Name: idx_works_inventory_requirement; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_works_inventory_requirement ON public.works USING btree (warehouse_id, operation_type, item_id, required_at) WHERE (operation_type = ANY (ARRAY['INBOUND'::text, 'OUTBOUND'::text]));


--
-- Name: idx_works_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_works_open ON public.works USING btree (warehouse_id, status, priority, scheduled_start);


--
-- Name: ux_conversation_sequence; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_conversation_sequence ON public.conversation_command_link USING btree (conversation_id, sequence_number);


--
-- Name: automatic_replan_request fk_auto_replan_event; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automatic_replan_request
    ADD CONSTRAINT fk_auto_replan_event FOREIGN KEY (event_id) REFERENCES public.execution_event_processing(event_id);


--
-- Name: clarification_request fk_clarification_command; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clarification_request
    ADD CONSTRAINT fk_clarification_command FOREIGN KEY (command_id) REFERENCES public.command_history(command_id);


--
-- Name: conversation_command_link fk_conversation_link_command; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_command_link
    ADD CONSTRAINT fk_conversation_link_command FOREIGN KEY (command_id) REFERENCES public.command_history(command_id);


--
-- Name: conversation_command_link fk_conversation_link_session; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_command_link
    ADD CONSTRAINT fk_conversation_link_session FOREIGN KEY (conversation_id) REFERENCES public.conversation_session(conversation_id);


--
-- Name: work_event fk_event_robot; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_event
    ADD CONSTRAINT fk_event_robot FOREIGN KEY (robot_id) REFERENCES public.robot(robot_id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: work_event fk_event_work; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_event
    ADD CONSTRAINT fk_event_work FOREIGN KEY (work_id) REFERENCES public.works(work_id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: inbound_order_line fk_inbound_order_inventory_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbound_order_line
    ADD CONSTRAINT fk_inbound_order_inventory_item FOREIGN KEY (item_id) REFERENCES public.inventory_item(item_id) NOT VALID;


--
-- Name: inventory_movement fk_inventory_movement_inventory_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_movement
    ADD CONSTRAINT fk_inventory_movement_inventory_item FOREIGN KEY (item_id) REFERENCES public.inventory_item(item_id) NOT VALID;


--
-- Name: inventory_movement fk_inventory_movement_warehouse_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_movement
    ADD CONSTRAINT fk_inventory_movement_warehouse_item FOREIGN KEY (warehouse_item_id) REFERENCES public.warehouse_items(warehouse_item_id) NOT VALID;


--
-- Name: outbound_order_line fk_outbound_order_inventory_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbound_order_line
    ADD CONSTRAINT fk_outbound_order_inventory_item FOREIGN KEY (item_id) REFERENCES public.inventory_item(item_id) NOT VALID;


--
-- Name: planning_stage_log fk_planning_stage_command; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.planning_stage_log
    ADD CONSTRAINT fk_planning_stage_command FOREIGN KEY (command_id) REFERENCES public.command_history(command_id);


--
-- Name: robot_execution_dispatch fk_robot_execution_plan_approval; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.robot_execution_dispatch
    ADD CONSTRAINT fk_robot_execution_plan_approval FOREIGN KEY (plan_version) REFERENCES public.execution_plan_approval(plan_version);


--
-- Name: scenario_comparison fk_scenario_comparison_command; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenario_comparison
    ADD CONSTRAINT fk_scenario_comparison_command FOREIGN KEY (command_id) REFERENCES public.command_history(command_id);


--
-- Name: scenario_comparison_run fk_scenario_run_command; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenario_comparison_run
    ADD CONSTRAINT fk_scenario_run_command FOREIGN KEY (command_id) REFERENCES public.command_history(command_id);


--
-- Name: scenario_comparison_run fk_scenario_run_comparison; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenario_comparison_run
    ADD CONSTRAINT fk_scenario_run_comparison FOREIGN KEY (comparison_id) REFERENCES public.scenario_comparison(comparison_id);


--
-- Name: warehouse_items fk_warehouse_items_inventory_item; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse_items
    ADD CONSTRAINT fk_warehouse_items_inventory_item FOREIGN KEY (item_id) REFERENCES public.inventory_item(item_id) NOT VALID;


--
-- Name: work_dependencies fk_work_dependency_predecessor; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_dependencies
    ADD CONSTRAINT fk_work_dependency_predecessor FOREIGN KEY (predecessor_work_id) REFERENCES public.works(work_id);


--
-- Name: work_dependencies fk_work_dependency_successor; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_dependencies
    ADD CONSTRAINT fk_work_dependency_successor FOREIGN KEY (successor_work_id) REFERENCES public.works(work_id);


--
-- Name: work_schedule_constraints fk_work_schedule_constraint_work; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_schedule_constraints
    ADD CONSTRAINT fk_work_schedule_constraint_work FOREIGN KEY (work_id) REFERENCES public.works(work_id);


--
-- Name: works fk_works_robot; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.works
    ADD CONSTRAINT fk_works_robot FOREIGN KEY (assigned_robot_id) REFERENCES public.robot(robot_id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: inventory_reservation inventory_reservation_warehouse_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_reservation
    ADD CONSTRAINT inventory_reservation_warehouse_item_id_fkey FOREIGN KEY (warehouse_item_id) REFERENCES public.warehouse_items(warehouse_item_id);


--
-- Name: inventory_reservation inventory_reservation_work_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_reservation
    ADD CONSTRAINT inventory_reservation_work_id_fkey FOREIGN KEY (work_id) REFERENCES public.works(work_id);


--
-- PostgreSQL database dump complete
--

\unrestrict xIITSX0ZiavH3hsNlooskHA2qyOhy0IpGbDmAMIdfc0aKwBQgbtkpH0f5dKoaAH

