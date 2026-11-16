# Connecting machines (PLC → MQTT)

The platform expects **JSON events over MQTT** (see README → "Sending events"). Getting data out of
a PLC is deliberately *not* part of the core stack – it runs on an **edge gateway** close to the
machines (an industrial PC, a small Linux box or a VM in the OT network). This keeps the server
lean and lets every plant use the tools its automation team already knows.

```
 PLC (S7 / OPC UA / Modbus) ──► edge gateway (Node-RED, PLC MQTT client, …) ──MQTT/TLS:8883──► platform
```

## What the gateway has to do

1. Read a few signals per machine, typically:
   * a **"cycle running"** bit (or a part counter) → `PROCESS` START/END events,
   * **alarm** bits / an alarm code → `ERROR` START/END events,
   * optional **changeover** / **waiting** bits → `SETUP` / `STANDBY`,
   * optional **good / scrap counters** → `quantity_good` / `quantity_scrap` on the END event.
2. On every **edge** of a signal send one event; START and END of the same occurrence share one
   `event_uid`.
3. Publish with **QoS 1** to `oee/<site>/<asset_id>/events` over TLS (port 8883), user
   `MQTT_DEVICE_USER`, trusting `site_overrides/certs/ca.crt` (or your corporate CA).
4. Keep the gateway clock **NTP-synchronised** – `event_datetime` is the machine/gateway time.

If your signals have other names or values (e.g. `"state": "Störung"`), you do **not** need to
translate them on the gateway: map them declaratively in `site_overrides/oee_rules.yaml`
(`field_aliases`, `category_aliases`, `state_aliases`, `asset_aliases`).

## Option A – Node-RED on the gateway (recommended for most plants)

Low-code, large ecosystem of PLC drivers, easy to hand over to automation technicians.

1. Install Node-RED on the gateway ([nodered.org](https://nodered.org/docs/getting-started/)).
2. Install the driver nodes you need via *Manage palette*, e.g.
   `node-red-contrib-s7` (Siemens S7), `node-red-contrib-opcua` (OPC UA),
   `node-red-contrib-modbus` (Modbus TCP/RTU).
3. Import [`examples/edge/node-red-flow.json`](../examples/edge/node-red-flow.json)
   (*Menu → Import*). It contains:
   * a function node **"run bit → PROCESS events"** and **"alarm bit → ERROR events"** that turn
     a boolean signal into START/END events (edge detection, shared `event_uid`),
   * an **MQTT out** node with TLS config for the platform,
   * inject nodes that simulate the signals so you can test without a PLC.
4. Set the tab environment variables `ASSET_ID` / `SITE_ID`, the broker host, the device
   credentials (stored in Node-RED's credential store, never in the flow file) and upload `ca.crt`
   in the TLS config.
5. Click the inject nodes → the events appear in Grafana ("Latest stops", cycle times) within a
   minute. Then replace the inject nodes with your S7 / OPC UA / Modbus read node
   (polling e.g. every 100–500 ms; the function only reacts on changes).

Production tips:
* Enable a **persistent context store** in Node-RED's `settings.js`
  (`contextStorage: { default: { module: "localfilesystem" } }`) so an open START survives a
  gateway restart.
* MQTT QoS 1 + the broker's persistent session protect the server side; for long network outages
  on the gateway side buffer messages locally (e.g. `node-red-contrib-queue-gate` or a file
  buffer) – the platform accepts late events for up to 7 days.
* One function node per signal and machine is easiest to maintain; copy the node for more machines
  or drive it with `msg.asset` from a table.

## Option B – PLC or machine publishes MQTT directly

Many current controllers can publish MQTT themselves (e.g. MQTT client libraries for Siemens
S7-1500, CODESYS-based PLCs, many robot and CNC controllers, IIoT gateways from automation
vendors). Send the same JSON as above; field and value names can again be adapted with
`oee_rules.yaml`.

## Option C – existing SCADA / MES / historian

If the data already exists in a SCADA, MES or historian, export it as events (START/END per
state change) via its MQTT or scripting interface. Historical backfills up to 7 days old are
accepted; older data can be inserted directly into `core.events` with the `oee_app` role.

## Checking the connection

* Grafana → "Seconds since last event" shows whether data arrives.
* Rejected payloads (wrong JSON, unknown categories, implausible values) are listed in the
  "Pipeline errors" panel and in `core.error_logs` with the reason – valid events keep flowing.
