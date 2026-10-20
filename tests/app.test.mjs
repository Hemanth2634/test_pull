import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, correctionPayload, createApi, commandDescription, DepthWorkflow } from "../web/app.js";

function prediction(overrides = {}) {
  const near = { box: [10, 20, 50, 60], distance_cm: 42.125, source: "depth_region" };
  return {
    id: "frame-1", objects: [{ box: [100, 20, 150, 60], distance_cm: 100, source: "depth_region" }, near],
    selected: near, threshold_cm: 50, expires_at: 1120, ...overrides,
  };
}
function fixture(overrides = {}) {
  const calls = { predict: [], confirm: [], cancel: [] };
  const api = {
    async predict(blob) { calls.predict.push(blob); return prediction(); },
    async confirm(id, payload) {
      calls.confirm.push({ id, payload });
      const distance = payload.distance_cm ?? 42.125;
      return { id, distance_cm: distance, command: distance < 50 ? "MOVE_BACK" : distance > 50 ? "ROTATE" : "MOVE_SIDE", delivered: false, delivery_error: null };
    },
    async cancel(id) { calls.cancel.push(id); return { id, cancelled: true }; },
    ...overrides,
  };
  let clock = 1000;
  const workflow = new DepthWorkflow(api, { now: () => clock });
  return { workflow, api, calls, advance(seconds) { clock += seconds; } };
}

test("correction values are optional or positive centimetres", () => {
  assert.deepEqual(correctionPayload(""), {});
  assert.deepEqual(correctionPayload("  "), {});
  assert.deepEqual(correctionPayload("50.25"), { distance_cm: 50.25 });
  for (const value of ["no", "0", "-1", Infinity, NaN]) {
    assert.throws(() => correctionPayload(value), /greater than zero/);
  }
});

test("capture waits for a human, preserving the backend-selected closest region", async () => {
  const { workflow, calls } = fixture();
  await workflow.analyze(new Blob(["frame"], { type: "image/jpeg" }));
  assert.equal(workflow.phase, "pending");
  assert.equal(workflow.pending.selected.distance_cm, 42.125);
  assert.equal(workflow.canCapture, false);
  assert.equal(workflow.canConfirm, true);
  assert.equal(calls.confirm.length, 0);
  await assert.rejects(workflow.analyze(new Blob()), /Finish or discard/);
});

test("confirming unchanged distance submits no rounded replacement", async () => {
  const { workflow, calls } = fixture();
  await workflow.analyze(new Blob());
  const result = await workflow.confirm("");
  assert.deepEqual(calls.confirm, [{ id: "frame-1", payload: {} }]);
  assert.equal(result.command, "MOVE_BACK");
  assert.equal(workflow.phase, "idle");
  await assert.rejects(workflow.confirm(), /no unexpired prediction/);
  assert.equal(calls.confirm.length, 1);
});

test("a corrected depth controls the single symbolic command", async () => {
  const { workflow, calls } = fixture();
  await workflow.analyze(new Blob());
  const result = await workflow.confirm("50");
  assert.deepEqual(calls.confirm[0].payload, { distance_cm: 50 });
  assert.equal(result.command, "MOVE_SIDE");
  assert.match(workflow.message, /recorded as a symbolic command/);
  assert.doesNotMatch(workflow.message, /acknowledged/);
});

test("invalid correction preserves pending review and sends nothing", async () => {
  const { workflow, calls } = fixture();
  await workflow.analyze(new Blob());
  await assert.rejects(workflow.confirm("-2"), /greater than zero/);
  assert.equal(workflow.phase, "pending");
  assert.equal(calls.confirm.length, 0);
});

test("concurrent confirmation clicks cannot send two requests", async () => {
  let complete;
  let confirmations = 0;
  const { workflow } = fixture({ confirm: () => {
    confirmations += 1;
    return new Promise(resolve => { complete = resolve; });
  } });
  await workflow.analyze(new Blob());
  const first = workflow.confirm();
  assert.equal(workflow.phase, "confirming");
  await assert.rejects(workflow.confirm(), /no unexpired prediction/);
  complete({ command: "MOVE_BACK", distance_cm: 42, delivered: false, delivery_error: null });
  await first;
  assert.equal(confirmations, 1);
});

test("a failed confirmation is not retried because delivery may be unknown", async () => {
  let confirmations = 0;
  const { workflow } = fixture({ async confirm() { confirmations += 1; throw new ApiError("connection lost"); } });
  await workflow.analyze(new Blob());
  await assert.rejects(workflow.confirm(), /connection lost/);
  assert.equal(workflow.phase, "idle");
  assert.equal(workflow.pending, null);
  assert.match(workflow.message, /will not be retried/);
  await assert.rejects(workflow.confirm(), /no unexpired prediction/);
  assert.equal(confirmations, 1);
});

test("an expired prediction cannot produce a confirmation", async () => {
  const { workflow, calls, advance } = fixture();
  await workflow.analyze(new Blob());
  advance(121);
  await assert.rejects(workflow.confirm(), /no unexpired prediction/);
  assert.equal(workflow.phase, "idle");
  assert.equal(calls.confirm.length, 0);
  assert.deepEqual(calls.cancel, ["frame-1"]);
  assert.match(workflow.message, /expired/);
});

test("no depth regions enables recapture without any command", async () => {
  const { workflow, calls } = fixture({ async predict() { return prediction({ id: null, objects: [], selected: null, expires_at: null }); } });
  await workflow.analyze(new Blob());
  assert.equal(workflow.canCapture, true);
  assert.equal(workflow.canConfirm, false);
  assert.equal(calls.confirm.length, 0);
  assert.match(workflow.message, /No valid depth region/);
});

test("missing trained weights is shown honestly", async () => {
  const { workflow, calls } = fixture({ async predict() { throw new ApiError("Missing checkpoint", 503); } });
  await assert.rejects(workflow.analyze(new Blob()), /Missing checkpoint/);
  assert.match(workflow.message, /No trained depth model/);
  assert.equal(workflow.canCapture, true);
  assert.equal(calls.confirm.length, 0);
});

test("discard cancels pending review and does not confirm", async () => {
  const { workflow, calls } = fixture();
  await workflow.analyze(new Blob());
  await workflow.discard();
  assert.deepEqual(calls.cancel, ["frame-1"]);
  assert.equal(calls.confirm.length, 0);
  assert.equal(workflow.canConfirm, false);
});

test("a late prediction after stopping is cancelled rather than revived", async () => {
  let complete;
  const { workflow, calls } = fixture({ predict: () => new Promise(resolve => { complete = resolve; }) });
  const analysis = workflow.analyze(new Blob());
  await workflow.discard();
  complete(prediction());
  assert.equal(await analysis, null);
  assert.equal(workflow.phase, "idle");
  assert.equal(workflow.pending, null);
  assert.deepEqual(calls.cancel, ["frame-1"]);
  assert.equal(calls.confirm.length, 0);
});

test("delivery status distinguishes acknowledged, failed and symbolic commands", () => {
  const result = { command: "ROTATE", distance_cm: 70 };
  assert.match(commandDescription({ ...result, delivered: true }), /acknowledged by ESP32/);
  assert.match(commandDescription({ ...result, delivered: false, delivery_error: "timeout" }), /delivery failed or is unconfirmed/);
  assert.match(commandDescription({ ...result, delivered: false, delivery_error: null }), /recorded as a symbolic command/);
});

test("API uploads binary frames and sends explicit confirmation JSON", async () => {
  const calls = [];
  const api = createApi(async (path, options) => {
    calls.push({ path, options });
    return { ok: true, async json() { return { ok: true }; } };
  });
  const blob = new Blob(["jpeg"], { type: "image/jpeg" });
  await api.predict(blob);
  await api.confirm("frame id", { distance_cm: 51 });
  assert.equal(calls[0].path, "/api/predictions");
  assert.equal(calls[0].options.body, blob);
  assert.equal(calls[0].options.headers["Content-Type"], "image/jpeg");
  assert.equal(calls[1].path, "/api/predictions/frame%20id/confirm");
  assert.deepEqual(JSON.parse(calls[1].options.body), { distance_cm: 51 });
});

test("API errors preserve status and do not silently retry", async () => {
  let requests = 0;
  const api = createApi(async () => {
    requests += 1;
    return { ok: false, status: 410, async json() { return { detail: "Prediction expired" }; } };
  });
  await assert.rejects(api.confirm("frame-1", {}), error => error.status === 410 && /expired/.test(error.message));
  assert.equal(requests, 1);
});
