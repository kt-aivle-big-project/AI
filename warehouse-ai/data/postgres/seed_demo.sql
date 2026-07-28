--
-- PostgreSQL database dump
--

\restrict S9FZHxjHdbaBVuBeLI5YEPCFGRVxD5fexgLoouIhm14dHKfSmq2qxq3cAHvhEcB

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

--
-- Data for Name: inventory_item; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('ITEM-C', 'ITEM-C', 'BOX', true, '2026-07-23 09:31:13.102564+09', '2026-07-23 09:31:13.102564+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('ITEM-A', 'ITEM-A', 'BOX', true, '2026-07-23 09:31:13.102564+09', '2026-07-23 09:31:13.102564+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('ITEM-B', 'ITEM-B', 'BOX', true, '2026-07-23 09:31:13.102564+09', '2026-07-23 09:31:13.102564+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('A', 'Demo item A', 'BOX', true, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:34:52.934825+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('B', 'Demo item B', 'BOX', true, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:34:52.934825+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('C', 'Demo item C', 'BOX', true, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:34:52.934825+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('D', 'Demo item D', 'BOX', true, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:34:52.934825+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('E', 'Demo item E', 'BOX', true, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:34:52.934825+09');
INSERT INTO public.inventory_item (item_id, item_name, base_unit, active, created_at, updated_at) VALUES ('F', 'Demo item F', 'BOX', true, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:34:52.934825+09');


--
-- Data for Name: inbound_order_line; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.inbound_order_line (inbound_id, warehouse_id, item_id, quantity_boxes, expected_arrival_at, expected_available_at, actual_arrival_at, actual_available_at, status, storage_node_id, lot_id, warehouse_item_id, created_at, updated_at) VALUES ('DEMO-IN-2-A', 2, 'A', 50, '2026-07-25 07:00:00+09', '2026-07-25 07:10:00+09', NULL, NULL, 'INSPECTING', 2088, 'DEMO-LOT-A-02', NULL, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:35:01.532678+09');
INSERT INTO public.inbound_order_line (inbound_id, warehouse_id, item_id, quantity_boxes, expected_arrival_at, expected_available_at, actual_arrival_at, actual_available_at, status, storage_node_id, lot_id, warehouse_item_id, created_at, updated_at) VALUES ('DEMO-IN-2-B', 2, 'B', 100, '2026-07-25 07:00:00+09', '2026-07-25 07:10:00+09', NULL, NULL, 'INSPECTING', 2088, 'DEMO-LOT-B-02', NULL, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:35:01.532678+09');
INSERT INTO public.inbound_order_line (inbound_id, warehouse_id, item_id, quantity_boxes, expected_arrival_at, expected_available_at, actual_arrival_at, actual_available_at, status, storage_node_id, lot_id, warehouse_item_id, created_at, updated_at) VALUES ('DEMO-IN-2-F', 2, 'F', 20, '2026-07-25 07:00:00+09', '2026-07-25 07:10:00+09', NULL, NULL, 'INSPECTING', 2088, 'DEMO-LOT-F-02', NULL, '2026-07-23 09:48:50.03534+09', '2026-07-24 16:35:01.532678+09');


--
-- Data for Name: robot; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.robot (robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version) VALUES ('R-02', 1, 'R-02', 1, 93.00, 'IDLE', 60.00, 0.00, 5);
INSERT INTO public.robot (robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version) VALUES ('R-03', 1, 'R-03', 2, 76.00, 'IDLE', 40.00, 0.00, 5);
INSERT INTO public.robot (robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version) VALUES ('R-01', 1, 'R-01', 5, 82.00, 'IDLE', 50.00, 0.00, 5);
INSERT INTO public.robot (robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version) VALUES ('R2-02', 2, 'R2-02', 2146, 93.58, 'IDLE', 50.00, 0.00, 5);
INSERT INTO public.robot (robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version) VALUES ('R2-03', 2, 'R2-03', 2152, 90.00, 'IDLE', 50.00, 0.00, 4);
INSERT INTO public.robot (robot_id, warehouse_id, robot_code, node_id, battery, status, max_load, current_load, version) VALUES ('R2-01', 2, 'R2-01', 2146, 100.00, 'IDLE', 50.00, 0.00, 10);


--
-- Data for Name: warehouse_items; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('WI-001', 1, 'ITEM-A', 'LOT-A-01', 3, 50, 0, '2026-08-10', 0, 'AVAILABLE', NULL, NULL, NULL, 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('WI-002', 1, 'ITEM-A', 'LOT-A-02', 4, 40, 0, '2026-09-15', 0, 'AVAILABLE', NULL, NULL, NULL, 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('WI-003', 1, 'ITEM-B', 'LOT-B-01', 6, 80, 0, '2026-10-01', 0, 'AVAILABLE', NULL, NULL, NULL, 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('WI-004', 1, 'ITEM-C', 'LOT-C-01', 7, 30, 0, NULL, 0, 'AVAILABLE', NULL, NULL, NULL, 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('DEMO-INV-2-A', 2, 'A', 'DEMO-LOT-A-01', 2088, 40, 0, '2026-08-22', 8, 'AVAILABLE', '2026-07-24 15:34:52.934825+09', '2026-07-24 15:35:01.532678+09', '2026-08-23 16:34:52.934825+09', 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('DEMO-INV-2-B', 2, 'B', 'DEMO-LOT-B-01', 2088, 20, 0, '2026-08-22', 6, 'AVAILABLE', '2026-07-24 15:34:52.934825+09', '2026-07-24 15:35:01.532678+09', '2026-08-23 16:34:52.934825+09', 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('DEMO-INV-2-D', 2, 'D', 'DEMO-LOT-D-01', 2088, 15, 0, '2026-08-22', 6, 'AVAILABLE', '2026-07-24 15:34:52.934825+09', '2026-07-24 15:35:01.532678+09', '2026-08-23 16:34:52.934825+09', 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('DEMO-INV-2-E', 2, 'E', 'DEMO-LOT-E-01', 2088, 120, 0, '2026-08-22', 6, 'AVAILABLE', '2026-07-24 15:34:52.934825+09', '2026-07-24 15:35:01.532678+09', '2026-08-23 16:34:52.934825+09', 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('DEMO-INV-2-F', 2, 'F', 'DEMO-LOT-F-01', 2088, 30, 0, '2026-08-22', 6, 'AVAILABLE', '2026-07-24 15:34:52.934825+09', '2026-07-24 15:35:01.532678+09', '2026-08-23 16:34:52.934825+09', 'BOX');
INSERT INTO public.warehouse_items (warehouse_item_id, warehouse_id, item_id, lot_id, node_id, quantity, reserved_quantity, expiry_date, version, status, received_at, available_at, expiration_at, base_unit) VALUES ('DEMO-INV-2-C', 2, 'C', 'DEMO-LOT-C-01', 2088, 60, 0, '2026-08-22', 14, 'AVAILABLE', '2026-07-24 15:34:52.934825+09', '2026-07-24 15:35:01.532678+09', '2026-08-23 16:34:52.934825+09', 'BOX');


--
-- Data for Name: works; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.works (work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version, actual_started_at, actual_completed_at, operation_type, quantity_boxes, required_at, allow_partial_fulfillment, inventory_order_id) VALUES ('DEMO-W-OUT-2-F', 2, 'OUTBOUND', 'F', 50, 2088, 2146, 1, 'NEW', NULL, '2026-07-25 06:55:00+09', '2026-07-25 07:00:00+09', 10, NULL, NULL, 'OUTBOUND', 50, '2026-07-25 07:00:00+09', false, 'DEMO-OUT-2-F');
INSERT INTO public.works (work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version, actual_started_at, actual_completed_at, operation_type, quantity_boxes, required_at, allow_partial_fulfillment, inventory_order_id) VALUES ('DEMO-W-OUT-2-A', 2, 'OUTBOUND', 'A', 30, 2088, 2146, 10, 'NEW', NULL, '2026-07-25 01:25:00+09', '2026-07-25 01:30:00+09', 14, '2026-07-24 10:08:16.821159+09', '2026-07-24 10:08:16.821177+09', 'OUTBOUND', 30, '2026-07-25 01:30:00+09', false, 'DEMO-OUT-2-A');
INSERT INTO public.works (work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version, actual_started_at, actual_completed_at, operation_type, quantity_boxes, required_at, allow_partial_fulfillment, inventory_order_id) VALUES ('P16-W-OUT-2-C-001', 2, 'OUTBOUND', 'C', 1, 2088, 2146, 10, 'READY', 'R2-01', '2026-07-27 16:20:24.040223+09', '2026-07-27 16:22:34.040223+09', 37, '2026-07-27 13:22:37.816889+09', '2026-07-27 16:07:56.354233+09', 'OUTBOUND', 1, '2026-07-27 16:49:52.175954+09', false, 'P16-OUT-2-C-001');
INSERT INTO public.works (work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version, actual_started_at, actual_completed_at, operation_type, quantity_boxes, required_at, allow_partial_fulfillment, inventory_order_id) VALUES ('W-001', 1, 'OUTBOUND', 'ITEM-A', 20, 3, 9, 1, 'NEW', NULL, '2026-07-22 15:14:27.118698+09', '2026-07-22 16:14:27.118698+09', 6, NULL, NULL, NULL, NULL, NULL, false, NULL);
INSERT INTO public.works (work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version, actual_started_at, actual_completed_at, operation_type, quantity_boxes, required_at, allow_partial_fulfillment, inventory_order_id) VALUES ('W-002', 1, 'OUTBOUND', 'ITEM-B', 30, 6, 9, 10, 'NEW', NULL, '2026-07-22 15:24:27.118698+09', '2026-07-22 16:24:27.118698+09', 6, NULL, NULL, NULL, NULL, NULL, false, NULL);
INSERT INTO public.works (work_id, warehouse_id, task_code, item_id, quantity, source_node, target_node, priority, status, assigned_robot_id, scheduled_start, scheduled_end, version, actual_started_at, actual_completed_at, operation_type, quantity_boxes, required_at, allow_partial_fulfillment, inventory_order_id) VALUES ('W-003', 1, 'INBOUND', 'ITEM-C', 15, 8, 7, 50, 'NEW', NULL, '2026-07-22 15:34:27.118698+09', '2026-07-22 16:34:27.118698+09', 6, NULL, NULL, NULL, NULL, NULL, false, NULL);


--
-- Data for Name: inventory_reservation; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: outbound_order_line; Type: TABLE DATA; Schema: public; Owner: -
--

INSERT INTO public.outbound_order_line (outbound_id, warehouse_id, item_id, requested_quantity_boxes, required_by, priority, allow_partial_fulfillment, status, work_id, created_at, updated_at) VALUES ('DEMO-OUT-2-F', 2, 'F', 50, '2026-07-25 07:00:00+09', 'EMERGENCY', false, 'OPEN', 'DEMO-W-OUT-2-F', '2026-07-23 09:48:50.03534+09', '2026-07-24 16:35:01.532678+09');
INSERT INTO public.outbound_order_line (outbound_id, warehouse_id, item_id, requested_quantity_boxes, required_by, priority, allow_partial_fulfillment, status, work_id, created_at, updated_at) VALUES ('DEMO-OUT-2-A', 2, 'A', 30, '2026-07-25 01:30:00+09', 'NORMAL', false, 'OPEN', 'DEMO-W-OUT-2-A', '2026-07-23 09:48:50.03534+09', '2026-07-24 16:35:01.532678+09');
INSERT INTO public.outbound_order_line (outbound_id, warehouse_id, item_id, requested_quantity_boxes, required_by, priority, allow_partial_fulfillment, status, work_id, created_at, updated_at) VALUES ('P16-OUT-2-C-001', 2, 'C', 1, '2026-07-27 16:49:52.175954+09', 'NORMAL', false, 'OPEN', 'P16-W-OUT-2-C-001', '2026-07-27 12:51:04.907828+09', '2026-07-27 16:19:52.175954+09');


--
-- Data for Name: work_dependencies; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- Data for Name: work_schedule_constraints; Type: TABLE DATA; Schema: public; Owner: -
--



--
-- PostgreSQL database dump complete
--

\unrestrict S9FZHxjHdbaBVuBeLI5YEPCFGRVxD5fexgLoouIhm14dHKfSmq2qxq3cAHvhEcB

