# Meraki Voice Correctness, Hardening and UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four user-facing voice defects found in review (barge-in on finished turns, withheld TTS audio, over-eager echo filter, wrong speaking/idle state), harden the public server, and lift the UI from ~6.5/10 to ~8.5/10.

**Architecture:** Keep the existing shape (FastAPI + one WebSocket per browser, one cancellable turn task, ES-module frontend with no build step). Fix behaviour at the seams where it is wrong: `tts.stream_speech` delivery, `_Connection` barge-in and echo state, `SpeechPlayer` scheduling, and `app.js` lifecycle. Add small pure modules (`playback-gate.js`, `support.js`) so the new browser logic is testable with `node --test`.

**Tech Stack:** Python 3.12, FastAPI/Starlette, aiohttp, pytest (+ httpx TestClient), vanilla ES modules, `node --test`.

**Spec:** No separate spec. The binding requirements are (1) the merged code-review findings recorded in this conversation, summarised per task below, and (2) the user's decisions: scope = everything; session ids = validate and keep the `?s=` URL link; deliver on a branch + PR (never push to `main`); no optional UI extras (no example prompts, no copy-transcript, no model/voice footer).

## Global Constraints

- Work on branch `improve/voice-correctness-hardening-ui`. Never commit to or push `main`. Do not push at all; the controller pushes after final review and user testing.
- Both suites must stay green at the end of every task: `python -m pytest tests/ -q` and `node --test "tests/frontend/*.test.js"`. Tests are offline and need no API keys.
- TDD: write the failing test first, watch it fail, then implement.
- Frontend stays ES modules with no build step and no framework. The only external dependency is Google Fonts. Use `textContent`, never `innerHTML`, for anything model- or user-derived.
- Keys are per connection (`ApiKeys` on `_Connection`). Never assign keys to module state (`test_no_module_holds_keys_of_its_own` must keep passing).
- Everything backend is asyncio; no threads, no blocking I/O in `async def`.
- WebSocket frames are JSON with a `type` discriminator; the contract lives in the `meraki/protocol.py` docstring. Update it there whenever a message type or field changes.
- Match surrounding style: comment density, naming, docstrings that explain *why*. Keep changes surgical; do not refactor outside the task.
- Commit messages: imperative, explain why in the body, end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Windows host: use `python -m pytest`, forward slashes in git commands. The project root is `C:\Users\risha\OneDrive\Desktop\Projects\Updated MurfAI`.
- Exact constants decided by the controller: `BARGE_IN_MIN_WORDS = 2` (unchanged); echo needs at least 2 words to count as echo; `MAX_IN_FLIGHT = 3` (unchanged); session id pattern `^[A-Za-z0-9_-]{8,64}$`; WebSocket max frame `65536` bytes; handshake timeout stays 15 s; client connect timeout 20 s.

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `meraki/services/tts.py` | chunking, synthesis, ordered delivery | 1 |
| `meraki/main.py` | routes, `_Connection`, echo filter, barge-in, handshake | 2, 3, 4, 5 |
| `meraki/config.py`, `meraki/__init__.py` | settings, single-source version | 10 |
| `meraki/security.py` (new) | `SecurityHeadersMiddleware`, `StaticCacheMiddleware`, `origin_allowed()` | 4, 5 |
| `requirements.txt`, `requirements-dev.txt` (new), `render.yaml`, `run.py`, `.github/workflows/ci.yml` | deps, deploy flags, CI | 4, 5, 10 |
| `static/js/audio-player.js` | gapless scheduling, serialised decode, pending tracking | 6 |
| `static/js/playback-gate.js` (new) | pure "is the turn truly finished" decision | 6 |
| `static/js/support.js` (new) | feature detection, error mapping, `withTimeout` | 7 |
| `static/js/app.js`, `static/js/audio-capture.js` | state machine, barge-in, lifecycle, dialog, banner | 5, 7, 9 |
| `static/css/styles.css`, `templates/index.html`, `static/js/visualizer.js` | visual and accessibility pass | 8, 9 |
| `tests/test_tts_stream.py`, `tests/test_barge_in.py`, `tests/test_ws_http.py` (new) | new backend tests | 1, 3, 4, 5 |
| `tests/frontend/*.test.js` (new files) | new browser-logic tests | 6, 7 |
| `README.md`, `CLAUDE.md` | docs reconciled with code | 10 |

---

### Task 1: TTS delivers chunks as they finish; style fallback is scoped

**Why (review findings, backend #1 and #7, Minor "stream_speech cleanup"):** `stream_speech` (`meraki/services/tts.py:193-222`) only yields once `len(pending) > MAX_IN_FLIGHT - 1` or the LLM stream ends, so for 1-2 chunk replies the first audio waits for the whole LLM reply, and text frames stall because `tee()` is not pulled while the generator awaits a synth task. Separately, any Murf 400 flips the process-global `_style_supported`, degrading every visitor's voice until restart.

**Files:**
- Modify: `meraki/services/tts.py`
- Modify: `tests/test_tts_request.py` (style tests)
- Create: `tests/test_tts_stream.py`

**Interfaces:**
- Consumes: `chunk_stream(text_stream)`, `synthesize(session, api_key, text, voice_id)`, `MAX_IN_FLIGHT`.
- Produces: `stream_speech(session, api_key, text_stream, voice_id=VOICE_ID)` keeps its signature and still yields base64 MP3 `str` strictly in chunk order, but a finished chunk is yielded as soon as it and all earlier chunks are done, regardless of whether later text has arrived. `_style_supported` becomes a `set[tuple[str, str]]` named `_style_rejected` holding `(voice_id, MURF_MODEL)` pairs; `tests/test_tts_request.py`'s autouse fixture must clear it.

- [ ] **Step 1: Write failing tests in `tests/test_tts_stream.py`.** Use a fake `synthesize` (monkeypatch `tts.synthesize`) that sleeps a configurable delay and returns a marker string, and a fake text stream that yields tokens with delays. Required tests:
  - `test_first_audio_is_yielded_before_the_text_stream_ends`: text stream yields `"Hello there, friend. "` immediately then sleeps 0.6 s before yielding `"More text here."` and ending. Synth delay 0.05 s. Record `time.monotonic()` when the first chunk is yielded; assert it is < 0.4 s after start (it was ~0.6 s before the fix).
  - `test_chunks_are_yielded_in_order_even_if_a_later_one_finishes_first`: fake synth where chunk text "slow" takes 0.2 s and "fast" takes 0.01 s; assert yielded order is slow then fast.
  - `test_at_most_max_in_flight_synth_calls_run_at_once`: track concurrent count in the fake synth with 8 chunks; assert max concurrent <= `tts.MAX_IN_FLIGHT`.
  - `test_cancelling_the_consumer_cancels_pending_synthesis_and_the_text_stream`: start consuming, cancel the task mid-way; assert a flag set in the fake synth's `finally` fires and the fake text stream's `finally` ran (the generator is closed, not left for GC).
  - `test_a_synth_error_propagates_to_the_consumer`: fake synth raises `tts.TTSError("boom")` for the 2nd chunk; assert the consumer receives `TTSError` and no "Task exception was never retrieved" warning (run with `-W error::pytest.PytestUnraisableExceptionWarning` semantics by asserting via `pytest.warns` is not needed; simply assert the exception type and that all synth tasks are done afterwards).
- [ ] **Step 2: Run them; confirm the first fails.** `python -m pytest tests/test_tts_stream.py -v`.
- [ ] **Step 3: Rewrite `stream_speech`.** Design: a producer task iterates `chunk_stream(text_stream)` (wrapped in `contextlib.aclosing`) and, for each chunk, acquires a slot from an `asyncio.Semaphore(MAX_IN_FLIGHT)`, creates `asyncio.create_task(synthesize(...))` and puts the task on an `asyncio.Queue` (unbounded is fine because the semaphore bounds work); it puts a sentinel when the text ends, or the exception if `chunk_stream` raises. The semaphore slot is released in a done-callback of the synth task. The consumer loop `await queue.get()`s tasks in order, `yield await task`. In `finally`: cancel the producer, cancel every task still on the queue or in flight, and `await asyncio.gather(*tasks, return_exceptions=True)` so no exception goes unretrieved. Propagate a producer exception to the consumer after the already-queued chunks are yielded or immediately; either is acceptable, but a synth error must surface.
- [ ] **Step 4: Scope the style fallback.** Replace the module global `_style_supported` with `_style_rejected: set[tuple[str, str]]`. In `synthesize`, treat a 400 as a style rejection only when the response body contains the word `style` (case-insensitive) and the request was styled; add `(voice_id, MURF_MODEL)` to the set. Any other 400 raises `TTSError(f"Murf rejected the request: {detail}")` and does not touch the set. Update `tests/test_tts_request.py`: the fixture clears `tts._style_rejected`; the existing "dropped and retried" and "not retried once known" tests keep their bodies (`"style not supported ..."`); add `test_an_unrelated_400_does_not_disable_the_style` (body `"insufficient credits"`, assert `TTSError`, assert set still empty, and a following styled call still sends `style`).
- [ ] **Step 5: Run the full backend suite.** `python -m pytest tests/ -q` must pass.
- [ ] **Step 6: Commit** `fix: deliver TTS chunks as they finish and scope the style fallback`.

---

### Task 2: Echo filter matches words, needs two of them, and expires

**Why (review finding, backend #3):** `looks_like_echo` (`meraki/main.py:67-81`) does a character substring match ("no" matches inside "know", "yes" inside "yesterday") and `_spoken` is only cleared on the next `thinking` frame, so a short real answer can be dropped forever.

**Files:**
- Modify: `meraki/main.py` (`looks_like_echo`, `_Connection._spoken` handling in `_send` and `_pump_events`)
- Modify: `tests/test_echo.py`, `tests/test_connection.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `looks_like_echo(heard: str, spoken: str) -> bool` (same signature). New module constants in `meraki/main.py`: `ECHO_MIN_WORDS = 2`, `ECHO_CHARS_PER_SECOND = 12.0`, `ECHO_GRACE_SECONDS = 2.0`, and a module-level `_now = time.monotonic` indirection that tests can monkeypatch. `_Connection` gains `_echo_deadline: float` and a method `_echo_active() -> bool`; `_pump_events` passes `self._spoken if self._echo_active() else ""` to `looks_like_echo`.

- [ ] **Step 1: Write failing tests.** In `tests/test_echo.py` add:
  - `test_a_short_word_inside_a_longer_word_is_not_echo`: `not looks_like_echo("no wait", "I don't know yesterday")` and `not looks_like_echo("yes", "...yesterday...")`.
  - `test_a_single_word_is_never_echo`: `not looks_like_echo("no", "No, it is not.")`.
  - `test_whole_words_in_order_are_still_echo`: existing positive tests must still pass unchanged.
  - `test_a_phrase_spanning_a_word_boundary_does_not_match_mid_word`: `not looks_like_echo("ick is to", "The trick is to send")`.
  In `tests/test_connection.py` add expiry tests using the `drive()` helper extended with a `now` parameter that monkeypatches `meraki.main._now`:
  - `test_echo_stops_counting_after_the_reply_has_finished_playing`: set `_spoken` to a 40-char reply, mark the deadline passed, send a final that is a substring of it, assert a turn starts.
  - `test_echo_still_counts_while_the_reply_is_playing`: same but within the deadline, assert no turn.
- [ ] **Step 2: Run; confirm failures.**
- [ ] **Step 3: Implement.** `looks_like_echo`: normalise both; return False if `phrase` has fewer than `ECHO_MIN_WORDS` words; match on `f" {phrase} " in f" {norm_spoken} "`. Deadline: in `_send`, on `thinking` reset `_spoken = ""` and set `_echo_deadline = _now() + ECHO_GRACE_SECONDS`; on the first `audio` frame of a turn record `_first_audio_at = _now()`; on every `reply_chunk` append to `_spoken`. `_echo_active()` returns `_now() < max(self._echo_deadline, self._first_audio_at + len(self._spoken) / ECHO_CHARS_PER_SECOND + ECHO_GRACE_SECONDS)` when audio has started, else `_now() < self._echo_deadline`. Update the `looks_like_echo` docstring: document the two-word floor and the expiry, and that the cost is now "a verbatim repeat of 2+ words while it speaks".
- [ ] **Step 4: Run both suites.**
- [ ] **Step 5: Commit** `fix: echo filter matches whole words, needs two, and expires`.

---

### Task 3: Barge-in works after generation finishes; cancellation is safe

**Why (review findings, backend #2 and #4, Minor "close() not exception-safe", Important test gap #9; frontend C1):** `_cancel_turn` (`meraki/main.py:327-335`) returns without sending `interrupted` when the turn task is already done, but TTS finishes seconds before playback, so talking over a reply does nothing. A one-word final such as "stop" cancels silently. `with suppress(CancelledError): await turn` swallows a cancel aimed at the caller, which can hang `close()`. `test_a_single_word_partial_does_not_trip_barge_in` is vacuous.

**Files:**
- Modify: `meraki/main.py` (`_Connection._cancel_turn`, `_pump_events`, `_send`, `close`)
- Modify: `meraki/protocol.py` docstring (document that `interrupted` is also sent for a finished turn whose audio may still be playing)
- Create: `tests/test_barge_in.py`
- Modify: `tests/test_connection.py` (replace the vacuous test)

**Interfaces:**
- Consumes: `_Connection._echo_active() -> bool` from Task 2 (True while the last reply is still expected to be playing in the browser).
- Produces: `_Connection._audio_pending: bool` (True once an `audio` frame was sent for the latest turn and no `interrupted` has been sent since; reset only by sending `interrupted`), `_Connection._interrupt_sent: bool` (once-per-utterance guard, cleared when a `final` is processed). Behaviour: a partial with at least `BARGE_IN_MIN_WORDS` words, or any non-echo final, sends `interrupted` exactly once per utterance if (a) a turn task was running and was cancelled, or (b) `_audio_pending` is True **and** `_echo_active()` is True (so a long-finished reply does not trigger a spurious `interrupted`). The `final` branch sends `interrupted` (when applicable) before it creates the replacement turn task, and the client contract is unchanged.

- [ ] **Step 1: Write failing tests in `tests/test_barge_in.py`** using a fake websocket and `FakeSpeech` like `tests/test_connection.py` (reuse by importing the helpers or copy them; keep the file self-contained). Required tests:
  - `test_two_word_partial_cancels_a_running_turn_and_says_interrupted`: `turn_factory` blocks on an `asyncio.Event`; feed `[{"kind":"final","text":"tell me a story"}, {"kind":"partial","text":"no wait"}, {"kind":"closed"}]`; assert `"interrupted"` appears in sent types and the turn task was cancelled.
  - `test_two_word_partial_interrupts_audio_that_is_still_playing_after_the_turn_finished`: run a turn to completion with an `audio` frame sent (have the fake `_run_turn` call `await conn._send(protocol.audio(0, "QUJD"))` then return), then feed a 2-word partial; assert `interrupted` is sent.
  - `test_a_one_word_final_interrupts_playing_audio`: after the finished turn above, feed `{"kind":"final","text":"stop"}`; assert `interrupted` precedes the new turn starting (`started == [first, "stop"]` and the `interrupted` frame index is before the second `final` frame's turn).
  - `test_interrupted_is_sent_once_per_utterance`: feed three growing partials ("no wait", "no wait hang", "no wait hang on"); assert exactly one `interrupted`.
  - `test_nothing_is_interrupted_when_nothing_is_playing`: partials with no turn and no audio produce no `interrupted`.
  - `test_long_finished_audio_does_not_trigger_a_spurious_interrupted`: after a finished turn that sent audio, advance the monkeypatched `meraki.main._now` past the echo window, then feed a 2-word partial; assert no `interrupted`.
  - `test_cancelling_the_pump_while_it_awaits_a_turn_is_not_swallowed`: create a turn task that takes 0.2 s to unwind after cancel (catch `CancelledError` and `await asyncio.sleep(0.2)` before re-raising), run `conn._cancel_turn(notify=False)` inside a task, cancel that task, assert it ends cancelled (`task.cancelled()`) rather than completing normally, within 1 s.
  - `test_close_still_closes_the_socket_when_speech_close_raises`: `conn._speech` is a stub whose `close` raises `RuntimeError`; assert `FakeWebSocket.closed` is True after `await conn.close()` (the exception may propagate or be logged, but the socket must close).
- [ ] **Step 2: Replace the vacuous test** in `tests/test_connection.py`: `test_a_single_word_partial_does_not_trip_barge_in` must run a live turn first (blocking `turn_factory`) and then feed `{"kind":"partial","text":"um"}`; assert no `interrupted` and the turn is not cancelled. Verify it fails when `BARGE_IN_MIN_WORDS` is monkeypatched to 1.
- [ ] **Step 3: Run; confirm failures.**
- [ ] **Step 4: Implement.** In `_send`, set `_audio_pending = True` when `kind == "audio"` and reset `_audio_pending = False` after an `interrupted` frame. Rewrite `_cancel_turn(*, notify)` to: take and clear `self._turn`; if it exists and is not done, `turn.cancel()` then `await asyncio.wait({turn})` (this does not swallow a cancel aimed at the caller; if the turn raised a non-cancel exception, log it); decide `should_notify = notify and (was_running or self._audio_pending) and not self._interrupt_sent`; if so send `protocol.interrupted()` and set `_interrupt_sent = True`. In the `final` branch call `await self._cancel_turn(notify=True)` (so a one-word "stop" interrupts) and then clear `_interrupt_sent`. In the partial branch keep the 2-word threshold and `notify=True`. Make `close()` use `try/finally` so `ws.close()` always runs, and make `SpeechStream.close()` failures logged not raised there. Update the `protocol.py` docstring line for `interrupted`.
- [ ] **Step 5: Run the full backend suite.**
- [ ] **Step 6: Commit** `fix: barge-in interrupts audio still playing after the turn finished`.

---

### Task 4: Handshake validation, session ids, WebSocket limits and HTTP routes

**Why (review findings, backend #5, #6, #8, minors "TemplateResponse", "index() blocking rglob", frontend M12):** `{"keys":"abc"}` raises `AttributeError` inside `ApiKeys.from_payload`; a binary first frame raises `KeyError` in `receive_json`; both give a traceback and no error frame. `session_id` is unbounded and logged before validation; clients without one share an `"anonymous"` conversation. No Origin check, 16 MiB default WebSocket frame limit, default aiohttp connector limit of 100 with no connect timeout. `HEAD /` and `HEAD /health` return 405. `TemplateResponse(name, {"request": ...})` is deprecated. `_asset_version()` runs an `rglob` per request.

**Files:**
- Create: `meraki/security.py` (only `origin_allowed()` in this task), `requirements-dev.txt`
- Modify: `meraki/main.py`, `meraki/config.py` (`ApiKeys.from_payload` tolerance), `render.yaml`, `run.py`, `.claude/launch.json`, `.github/workflows/ci.yml` (install `requirements-dev.txt`)
- Create: `tests/test_ws_http.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `meraki.security.origin_allowed(origin: str | None, host: str, extra: set[str]) -> bool` (True when origin is None, or its host equals `host`, or the origin string is in `extra`); env var `MERAKI_ALLOWED_ORIGINS` (comma-separated, read once at import into `ALLOWED_ORIGINS` in `config.py`); `SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")` and `valid_session_id(s) -> bool` in `meraki/session.py`; `requirements-dev.txt` containing `-r requirements.txt`, `pytest`, `httpx`.

- [ ] **Step 1: Write failing tests in `tests/test_ws_http.py`** with `fastapi.testclient.TestClient(meraki.main.app)` used as a context manager (so lifespan runs). Required tests:
  - `test_non_dict_keys_in_the_handshake_is_a_typed_error`: send `{"type":"config","keys":"abc"}`; receive a frame with `type == "error"`, `fatal is True`, and the socket then closes. No unhandled exception (the server log must not contain a traceback; assert via `caplog` that no record has `exc_info`).
  - `test_a_binary_first_frame_is_a_typed_error`: `ws.send_bytes(b"\x00\x01")`; expect an error frame.
  - `test_handshake_with_no_keys_reports_each_missing_service`: existing behaviour preserved (error code `keys`, names Deepgram/Ollama/Murf).
  - `test_an_invalid_session_id_is_replaced_not_logged`: send an id of 5000 characters with newlines; with env keys faked as present and `SpeechStream.start` monkeypatched to raise `SpeechError("x")` (so the test stops before real Deepgram), assert `caplog.text` does not contain the raw id and no log line exceeds 300 characters.
  - `test_two_clients_without_an_id_do_not_share_a_conversation`: after two handshakes without `session_id`, `sessions` keys differ and neither is `"anonymous"`.
  - `test_history_routes_reject_malformed_ids`: `GET /api/history/ab` returns `{"history": []}` and does not create a session; `DELETE /api/history/ab` returns `{"cleared": False}`.
  - `test_origin_check`: a WebSocket with `origin: https://evil.example` header is closed with code 1008 before the handshake; no `Origin` header and a same-host Origin are accepted.
  - `test_head_requests_succeed`: `client.head("/")` and `client.head("/health")` return 200.
  - `test_index_does_not_walk_the_static_tree_per_request`: monkeypatch `meraki.main._compute_asset_version` with a counter; two requests to `/` call it once.
  Add unit tests for `origin_allowed` and `valid_session_id` in the same file.
- [ ] **Step 2: Run; confirm failures.** (Install `httpx` from `requirements-dev.txt` if missing.)
- [ ] **Step 3: Implement.**
  - `ApiKeys.from_payload(payload)`: if `payload` is not a `dict`, treat as `{}`.
  - `_handshake`: replace `receive_json()` with `receive()`, decode `text` via `json.loads` (wrap `ValueError`/`TypeError`/`KeyError`), and send the typed fatal error for non-text frames, non-dict JSON, or timeout. `session_id`: `sid = str(message.get("session_id") or "")`; if `valid_session_id(sid)` keep it, else `uuid.uuid4().hex`. Log only the validated id. Never use `"anonymous"`.
  - `GET`/`DELETE /api/history/{session_id}`: if not `valid_session_id`, return `{"history": []}` / `{"cleared": False}` without touching the store.
  - WebSocket route: before `accept()`, `if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host", ""), ALLOWED_ORIGINS): await websocket.close(code=1008); return`.
  - `_lifespan`: `aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=400), timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))`. The session-level `sock_connect` bounds the Deepgram `ws_connect`; additionally pass `timeout=aiohttp.ClientWSTimeout(ws_close=5)` in `SpeechStream.start`'s `ws_connect` call so teardown cannot block for aiohttp's 10 s default. Existing per-request timeouts in `llm.py` and `tts.py` stay as they are.
  - `index()`: compute `_asset_version()` once per process at import (`ASSET_V = _compute_asset_version()` lazily cached in a module global) instead of per request. Rename the function to `_compute_asset_version` and keep a thin cached accessor `_asset_version()` (the test above monkeypatches `_compute_asset_version`). While `--reload` is on, the module reloads on file change, so the token still changes in development.
  - Use `templates.TemplateResponse(request, "index.html", {...})` (drop `"request"` from the context).
  - Replace `@app.get("/")` and `@app.get("/health")` with `@app.api_route(path, methods=["GET", "HEAD"])`.
  - `render.yaml` startCommand and `run.py`/`.claude/launch.json`: add `--ws-max-size 65536`.
  - `requirements-dev.txt` and CI: install `-r requirements-dev.txt` in the backend job instead of `pip install pytest`.
- [ ] **Step 4: Run the full backend suite.**
- [ ] **Step 5: Commit** `fix: validate the handshake and session ids, bound the socket, answer HEAD`.

---

### Task 5: Security headers, static caching, key flag off the inline script, dependency bump

**Why (review findings, frontend I2 and I11, backend minors "dependency pins", "CI permissions"):** only `styles.css` and `app.js` carry `?v=`, the ES-module imports and the worklet URL do not, and `/static` sends no `Cache-Control`, so browsers may heuristically cache them for days. No security headers exist. The inline `<script>window.MERAKI_KEYS_REQUIRED = ...</script>` blocks a strict CSP. `fastapi 0.115.6` / `starlette 0.41.3` are old.

**Files:**
- Modify: `meraki/security.py` (add middleware), `meraki/main.py` (register), `templates/index.html`, `static/js/app.js` (line 9 only), `requirements.txt`, `.github/workflows/ci.yml` (`permissions: contents: read`), `render.yaml` (`PYTHON_VERSION` unchanged)
- Modify: `tests/test_ws_http.py`

**Interfaces:**
- Consumes: `origin_allowed` module `meraki/security.py` from Task 4 (keep it; add to the same file).
- Produces: `SecurityHeadersMiddleware` and `StaticCacheMiddleware` (pure ASGI classes, no new dependencies); `<body data-keys-required="true|false">` replaces `window.MERAKI_KEYS_REQUIRED`; `app.js` reads `document.body.dataset.keysRequired !== 'false'`.

- [ ] **Step 1: Write failing tests** in `tests/test_ws_http.py`: `test_every_response_has_security_headers` (assert `Content-Security-Policy` contains `default-src 'self'`, `frame-ancestors 'none'`, `connect-src 'self' ws: wss:`; `X-Content-Type-Options: nosniff`; `Referrer-Policy: no-referrer`; `Permissions-Policy` contains `microphone=(self)`); `test_static_files_revalidate` (`GET /static/js/app.js` has `Cache-Control: no-cache`); `test_the_page_has_no_inline_script` (response text contains no `<script>` without a `src=`); `test_keys_flag_is_a_body_data_attribute` (`data-keys-required="true"` when no env keys, `"false"` when all three env keys are monkeypatched present).
- [ ] **Step 2: Run; confirm failures.**
- [ ] **Step 3: Implement.** CSP: `default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' ws: wss:; media-src 'self' blob:; worker-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`. Headers added only if absent. `StaticCacheMiddleware` adds `Cache-Control: no-cache` to responses whose path starts with `/static/`. Remove the inline script from `index.html`, add `data-keys-required` to `<body>` (`{{ "true" if keys_required else "false" }}`), update `app.js:9`. Bump `requirements.txt`: run `python -m pip index versions fastapi` / `starlette` and choose the latest `fastapi` release whose pinned `starlette` range is compatible, update `fastapi`, `uvicorn[standard]`, `aiohttp`, `jinja2` to current patch releases of the same major lines, reinstall, and run both suites. If a bump breaks tests, pin the previous working version and say so in the report. Add `permissions: contents: read` at the top level of `ci.yml`.
- [ ] **Step 4: Verify manually** by starting the app with `python -m uvicorn meraki.main:app --port 8001` in the background, `curl -sI http://127.0.0.1:8001/static/js/app.js` and `/`, confirm the headers, then stop the server. Load the page in a browser if one is available and confirm there are no CSP violations in the console (report if you cannot).
- [ ] **Step 5: Run both suites.** Commit `feat: security headers, revalidating static files, refreshed dependencies`.

---

### Task 6: Player serialises decoding; speaking state comes from one gate

**Why (review findings, frontend C2, I1, M4):** `enqueue()` is async and un-awaited per frame, so chunks schedule in decode-completion order, not arrival order. A source only enters `sources` after `decodeAudioData` resolves, so `speech_done` arriving in the same burst as the last `audio` frame sees `player.playing === false` and flips the UI to "Listening" while audio is about to play. `onIdle` also fires on gaps between chunks mid-reply, flickering the state.

**Files:**
- Modify: `static/js/audio-player.js`
- Create: `static/js/playback-gate.js`
- Modify: `static/js/app.js` (wire the gate; handlers for `thinking`, `audio`, `speech_done`, `interrupted`, `error`)
- Create: `tests/frontend/playback-gate.test.js`
- Modify: `tests/frontend/audio-player.test.js`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SpeechPlayer.enqueue(base64): Promise<void>` unchanged signature but internally serialised (a `this.tail` promise chain; each call decodes and schedules strictly in call order; a failing decode does not break the chain); `SpeechPlayer.pending: number` (chunks queued or decoding); `SpeechPlayer.playing` now returns `this.sources.size > 0 || this.pending > 0`; `flush()` also resets `pending` handling safely (generation guard already prevents stale scheduling; pending counts are decremented when each queued task finishes or is skipped). `PlaybackGate` in `static/js/playback-gate.js`:
  ```js
  export class PlaybackGate {
    constructor({ isPlaying, onSettled })
    turnStarted()   // turnOpen = true
    turnEnded()     // turnOpen = false, then check()
    playerIdle()    // check()
    cancel()        // turnOpen = false, no callback
    check()         // if (!turnOpen && !isPlaying()) onSettled()
  }
  ```

- [ ] **Step 1: Write failing tests.** In `tests/frontend/audio-player.test.js` extend the fake context so `decodeAudioData` can be controlled per call (return a promise the test resolves manually). Add: `chunks are scheduled in arrival order even when a later decode finishes first` (enqueue A (3 s) and B (1 s) without awaiting, resolve B's decode first, then A's; assert `ctx.started[0]` is A's duration and B starts at A's start + 3), `playing is true while a chunk is still decoding`, `flush discards chunks that are still queued behind a decode`, and `a failed decode does not block the chunks behind it`. In `tests/frontend/playback-gate.test.js` add: settled only when turn ended AND player not playing; `playerIdle` mid-turn (turn open) does not settle; `turnEnded` while the player is still playing waits for `playerIdle`; `cancel` suppresses settlement; two turns in a row each settle once.
- [ ] **Step 2: Run `node --test "tests/frontend/*.test.js"`; confirm failures.**
- [ ] **Step 3: Implement the player changes** (promise chain, `pending` counter incremented synchronously in `enqueue` and decremented in a `finally`, `playing` getter, call `onIdle` only when `sources` empties AND `pending === 0`).
- [ ] **Step 4: Implement `PlaybackGate`** and wire `app.js`: create `gate = new PlaybackGate({ isPlaying: () => player.playing, onSettled: () => { if (recording) setState('listening','Listening'); else restIdle(); } })` where the player exists (inside `startRecording`, after the player is created); player `onIdle` calls `gate.playerIdle()`. Handlers: `thinking` → `gate.turnStarted()`; `speech_done` → `gate.turnEnded()` (replacing the immediate `player.playing` check); `interrupted` and fatal `error` → `gate.cancel()`; non-fatal `error` while a turn was open → `gate.turnEnded()`. The `audio` handler keeps `setState('speaking','Speaking')`.
- [ ] **Step 5: Run both suites.** Commit `fix: serialise chunk decoding and settle the speaking state in one place`.

---

### Task 7: Client barge-in, lifecycle robustness and support detection

**Why (review findings, frontend C1 client half, I3-I7, M5-M7, M10):** the client never flushes on its own, so a user's `final` while the old reply's audio is still queued makes the new reply queue behind it. Stale socket handlers act on newer sessions; `stopRecording`/`startRecording` race; `connect()` has no timeout; a socket closing during the mic prompt leaves a dead session; the mic `AudioContext` is created after a network round trip (Safari/iOS); the Firefox sample-rate fallback never triggers; no feature detection; raw error text; key whitelist; Space auto-repeat; `interrupted` forces "Listening" when not recording; `clearHistory` ignores failures and does not stop playback.

**Files:**
- Create: `static/js/support.js`, `tests/frontend/support.test.js`
- Modify: `static/js/app.js`, `static/js/audio-capture.js`

**Interfaces:**
- Consumes: `PlaybackGate` and the serialised `SpeechPlayer` from Task 6; `player.flush()`, `player.playing`.
- Produces in `static/js/support.js`:
  ```js
  export function detectSupport(env = globalThis)  // -> { ok: true } | { ok: false, reason: string }
  export function describeStartError(error)        // -> string, plain language
  export function withTimeout(promise, ms, message) // rejects Error(message) after ms; clears timer
  ```
  `detectSupport` checks, in order: `env.isSecureContext`, `env.navigator?.mediaDevices?.getUserMedia`, `env.AudioContext || env.webkitAudioContext`, `env.AudioWorkletNode`, `env.WebSocket`, and returns the first missing item's plain-language reason (e.g. "Microphone access needs a secure connection (https or localhost).", "This browser cannot capture audio.", "This browser does not support the audio features Meraki needs."). `describeStartError` maps `NotAllowedError`/`SecurityError` → "Microphone access was blocked.", `NotFoundError`/`OverconstrainedError` → "No microphone was found.", `NotReadableError` → "The microphone is busy or unavailable.", and otherwise `error.message || 'Could not start.'`.

- [ ] **Step 1: Write failing tests in `tests/frontend/support.test.js`** for every branch above, plus `withTimeout` resolving with the value when the promise settles first, rejecting with the given message when it does not (use `ms = 20`), and not leaving a pending timer (assert via `node:test`'s mock timers or by awaiting past the timeout without an unhandled rejection).
- [ ] **Step 2: Implement `support.js`.** Run the frontend suite.
- [ ] **Step 3: `app.js` barge-in.** `case 'final'`: `player.flush()` then set `listening` state if recording (the old reply is cut; the server already dropped echo before sending this frame). `case 'partial'`: if `player.playing` and the text has at least 2 words (`message.text.trim().split(/\s+/).length >= 2`), `player.flush()`, `gate.cancel()`, `replyBuffer = ''`, `showLive({reply: ''})`, `setState('listening','Listening')`. `case 'thinking'`: `player.flush()` before `gate.turnStarted()`. `case 'interrupted'`: only call `setState('listening', ...)` when `recording`; otherwise `restIdle()`.
- [ ] **Step 4: `app.js` lifecycle.** In `connect()` capture `const ws` and in `onmessage`/`onclose` return early when `ws !== socket && settled` (a stale socket must not touch global state). Wrap the connect in `withTimeout(connect(), 20000, 'The server took too long to answer.')` (on timeout also close the pending socket). Add a module-level `let generation = 0` incremented on each `startRecording`; every `await` in `startRecording` re-checks `generation` and bails (releasing anything it created) if a stop or newer start intervened; `stopRecording` sets `mic`/`socket` to null *before* awaiting `mic.stop()` (capture the local reference first). After `mic.start()` check `socket && socket.readyState === WebSocket.OPEN`, else throw `new Error('The connection dropped while starting.')`. Call `detectSupport()` at boot; when not ok, disable the mic button, set the hint to the reason, and set the state chip to a blocked state. Use `describeStartError` in the catch. Send only whitelisted keys: `keys: pickKeys(loadKeys())` where `pickKeys` copies only `KEY_FIELDS` entries that are non-empty strings. Guard the Space handler with `if (event.repeat) return;`. `clearHistory`: `try { const r = await fetch(...DELETE); if (!r.ok) throw ... } catch { toast('Could not clear the conversation.', 'error'); return; }`, then `player?.flush()` and `gate?.cancel()`. Show a toast on an unclean close only when `!event.wasClean` as now, and also when a clean close happened while recording and the user did not press stop (`recording` still true).
- [ ] **Step 5: `audio-capture.js`.** Add `async prepare()` that synchronously (before any await) creates the `AudioContext` (try 16 kHz, fall back to default) and is called first thing in `startRecording` inside the click gesture; `start()` reuses it. Wrap `createMediaStreamSource` in try/catch: on failure, close the context, recreate at the default rate and retry once (the worklet already resamples via `processorOptions.inputRate`). After obtaining the stream, set `track.onended = () => this.onEnded?.()`; `MicCapture` accepts an `onEnded` callback and `app.js` passes one that calls `stopRecording({silent:true})` and toasts "The microphone was disconnected." Also stop tracks and close the context if `start()` throws part-way.
- [ ] **Step 6: Run both suites; verify in the browser preview** (`preview_start` name `meraki`) that the page loads with no console errors and the mic button is enabled on `localhost`. Commit `fix: client-side barge-in, race-free start/stop, feature detection`.

---

### Task 8: Visual and accessibility pass (no behaviour changes)

**Why (review findings, frontend I8-I10, M1-M3, M9, M15, M16, UI items 1, 2, 9, 10, 11):** low-contrast small text (`--text-faint` 2.9-3.3:1), noisy live regions, missing focus rings, headline hidden under 700 px height, quiet idle meter, grey mic button, dead states in the visualizer, stale colour fallbacks, `getComputedStyle` per frame, no reduced-motion for the canvas, page cannot reflow in short viewports, unencoded favicon, missing `noscript`/`theme-color`.

**Files:**
- Modify: `static/css/styles.css`, `templates/index.html`, `static/js/visualizer.js`
- Create: `tests/frontend/visualizer.test.js` (small, for the pure parts)

**Interfaces:**
- Consumes: DOM ids used by `app.js` (`status`, `status-dot`, `meter`, `mic-btn`, `mic-label`, `hint`, `live`, `live-user`, `live-reply`, `transcript`, `empty`, `clear-btn`, `settings`, `settings-form`, `settings-btn`, `close-settings`, `forget-keys`, `toasts`, `key-deepgram|ollama|murf`). Do not rename or remove any id.
- Produces: `.mic` as the primary action; a `.mic__icon--stop` swap handled by CSS from `[data-active="true"]`; `Visualizer` exposes `static idleFloor` and uses cached colours.

- [ ] **Step 1: CSS.**
  - Raise `--text-faint` from `#56656e` to `#8393a0` (verify >= 4.5:1 on `--bg`, `--panel`, `--panel-2` with a quick script and put the ratios in your report); add `--accent-text: #ff6b72` for red text on dark panels (error toast, `.chip--status[data-state="blocked"]`, `.stage__eyebrow`, `.turn--assistant .turn__who`) and verify >= 4.5:1 on `--panel-2`.
  - Add `:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }` for `.mic`, `.ghost`, `.chip--btn`, `.primary`, `.field input`, `.field__get`; keep `.field input:focus { border-color: var(--accent); }` and add the outline.
  - `.mic`: at rest a visible accent border (`1px solid var(--accent-strong)`), subtle accent-tinted background (`var(--accent-soft)`), `min-height: 48px`; `min-height: 52px` under `(max-width: 720px)`. Active state unchanged (solid accent). Swap the icon to a stop square when `data-active="true"` using two inline SVGs in the template toggled by CSS (`display: none`/`block`), no JS needed.
  - `.ghost`, `.chip--btn`: `min-height: 40px` under `(pointer: coarse)`.
  - Change `@media (max-height: 700px)` to `(max-height: 560px)`. Add `@media (max-height: 520px)`: `body { height: auto; min-height: 100dvh; overflow-y: auto; }`, `.log { min-height: 220px; }`. Add a `100vh` fallback line before each `100dvh`.
  - `.foot { padding-bottom: calc(12px + env(safe-area-inset-bottom)); }` (and the mobile variant).
  - `.field__get`: restyle as a visible text link on its own line below the input (static position, 12px, underline on hover) and drop the absolute positioning.
  - New-turn fade: `.turn { animation: turn-in 220ms ease both; }` with `@keyframes turn-in { from { opacity: 0; transform: translateY(4px) } }`; the existing reduced-motion block already disables it.
- [ ] **Step 2: Template.**
  - `#live`: `aria-live="off"`; `#transcript`: `role="log" aria-live="polite" aria-label="Conversation transcript"`; the chip text span `#status` gets `role="status"` (its parent keeps no live role); `.log` section gets `aria-label="Conversation"`; `#toasts` becomes `aria-live="polite"` (errors will be given `role="alert"` by the toast function in Task 9).
  - `#settings` dialog: `aria-labelledby="settings-title"` with `id="settings-title"` on its `h2`; move each "Get one" link out of its `<label>` into a sibling element with `aria-label="Get a Deepgram key"` (and Ollama, Murf); inputs get `autocomplete="new-password"`; remove `<noscript>`-less gap by adding `<noscript><p class="noscript">Meraki needs JavaScript to run.</p></noscript>` inside `<main>`; add `<meta name="theme-color" content="#07090b">`; replace the data-URL favicon with a percent-encoded SVG (`%3Csvg ... %23` encoding of `<`, `>`, `#`) of the brand spider mark in `--accent` red.
  - Mic button markup: two icons inside `.mic` (mic and stop), plus `aria-pressed="false"` (Task 9 toggles it in JS).
- [ ] **Step 3: Visualizer.** Remove the dead `'error'` state test. Handle `blocked` and `connecting` in `step()` (treat as idle). Read `--accent` and `--bar-idle` once in the constructor and again in `refreshColors()` (called on `resize` and when `matchMedia('(prefers-color-scheme: dark)')` changes); fall back to the real values `#e8434a` and `#212b31`. Raise the idle floor: idle targets `0.07 + wave * 0.025`; idle bar colour from CSS `--bar-idle` brightened to `#33414a`. When `matchMedia('(prefers-reduced-motion: reduce)').matches`, idle and thinking use a static low profile (no per-frame sin) and `draw()` is skipped unless the state changed. Pause the rAF loop when `document.hidden`.
- [ ] **Step 4: Tests.** `tests/frontend/visualizer.test.js`: stub `document`/`getComputedStyle`/`matchMedia`/canvas context minimally and assert (a) idle targets are >= 0.045 for every bar, (b) `setSpectrum` is ignored when idle, (c) colours are read once per `refreshColors()` and not once per `draw()` (count `getComputedStyle` calls across 10 `draw()`s), (d) with reduced motion the idle targets are constant across `step()` calls.
- [ ] **Step 5: Verify in the browser preview** at 1280x800, 375x812, and 667x375 (landscape): headline visible at 375x812, mic reachable at 667x375 via scroll, no console errors. Run both suites. Commit `feat: contrast, focus, live regions and layout polish`.

---

### Task 9: Interactive UI: key dialog, error banner with retry, cold start, stop-speaking, inline states

**Why (review findings, UI items 3-8 and 10, frontend M7-M9):** the key dialog gives errors via toasts that render behind the modal, the "Needs keys" chip is not clickable, no persistent error state or retry, a Render cold start just says "Could not reach the server.", no manual stop while speaking, no loading state for history.

**Files:**
- Modify: `templates/index.html`, `static/css/styles.css`, `static/js/app.js`

**Interfaces:**
- Consumes: Task 8's markup (aria attributes, icons, link placement) and Task 7's `withTimeout`, `describeStartError`, `PlaybackGate` wiring. Keep all existing element ids; new ids: `banner`, `banner-text`, `banner-retry`, `stop-speaking`, `key-error-deepgram`, `key-error-ollama`, `key-error-murf`, `key-step-deepgram`, `key-step-ollama`, `key-step-murf`, `toggle-keys`.
- Produces: toast function accepts `kind`; `showBanner(message, { retry: boolean })` and `hideBanner()`.

- [ ] **Step 1: Key dialog.**
  - Add a three-step checklist row at the top (`Deepgram`, `Ollama`, `Murf`), each with a status mark (`•` pending, `✓` when that field has text), updated on `input` events and when the dialog opens.
  - Inline error `<p class="field__error" id="key-error-…" role="alert" hidden>` under each input; `submitSettings` shows "Paste your <Service> key." for each empty field when `KEYS_REQUIRED` and focuses the first invalid field instead of toasting.
  - `openSettings()` focuses the first empty field (or the first field if none are empty).
  - A single "Show" / "Hide" toggle button (`#toggle-keys`, `type="button"`, `aria-pressed`) switching the three inputs between `type="password"` and `type="text"`.
  - Escape (native) and a click on the `::backdrop` (listen for `click` on the dialog where `event.target === dialog`) close the dialog.
  - Toasts raised while the dialog is open still show, but dialog validation uses only inline errors.
- [ ] **Step 2: Status chip as a control.** When the state is `blocked` ("Needs keys"), the chip's text is wrapped in or replaced by a `<button type="button" class="chip chip--status chip--action">` behaviour: make `.chip--status` keyboard-activatable (`tabindex="0"`, `role="button"` only while blocked, Enter/Space opens settings) and add the cursor/hover style. Remove the role/tabindex again when not blocked (set in `setState`).
- [ ] **Step 3: Error banner with retry.** Add `<div id="banner" class="banner" role="alert" hidden><span id="banner-text"></span><button id="banner-retry" class="ghost" type="button">Retry</button></div>` between the stage and the log. For fatal errors (`message.fatal`, connect failures in `startRecording`'s catch), call `showBanner(message, { retry: true })` in addition to the toast; Retry calls `hideBanner()` then `startRecording()`. Any successful start, or the user pressing the mic, hides it. `Needs keys` problems show the banner without Retry and with an "Open Keys" action instead (reuse the same button with changed text and handler).
- [ ] **Step 4: Cold start.** In `startRecording`, after 5 s in the `connecting` state without `ready`, show the status label "Waking the server…" and the hint "A sleeping free server can take up to a minute." On a connect failure that is a network-level failure (the `onerror`/timeout paths, not a server `error` frame), retry the connect once automatically after 2 s before surfacing the failure; surface a normal failure for key and handshake errors (no retry).
- [ ] **Step 5: Stop-speaking and hints.** Add `<button id="stop-speaking" class="ghost" type="button" hidden>Stop speaking</button>` beside the hint; shown while `uiState === 'speaking'`; click does `player.flush()`, `gate.cancel()`, `replyBuffer = ''`, `showLive({reply: ''})`, `setState('listening','Listening')` (the server's turn may still be running; send nothing, the next utterance's `final` replaces it). Hint text: while listening "Just talk. Interrupt any time."; while speaking "Talk over it to interrupt."; idle "Click to start a conversation. Space also works." Toggle `aria-pressed` on the mic button in `startRecording`/`stopRecording`.
- [ ] **Step 6: History loading and toasts.** `loadHistory` sets `ui.empty.textContent = 'Loading conversation…'` while fetching and restores the original text after; preserve scroll position when the transcript is replaced (restore `scrollTop` if the user had scrolled up). `toast()` gives `role="alert"` to `error` toasts and `role="status"` otherwise. `forgetKeys` also clears the checklist marks.
- [ ] **Step 7: Verify in the browser preview** (desktop and 375px): open the dialog with no keys (temporarily unset env keys by starting with `DEEPGRAM_API_KEY=` etc. set blank, or test the dialog directly via `openSettings`), confirm focus lands in the first field, inline errors appear, checklist updates, Escape closes; confirm the blocked chip opens it; confirm the banner and Retry show on a forced failure (point the page at a closed port by temporarily stubbing `socketUrl` through the console and restore it); take screenshots of dialog, banner, and speaking states (the speaking state can be forced from the console via `setState('speaking','Speaking')` since real audio needs keys). Run both suites. Commit `feat: guided key dialog, persistent errors with retry, cold-start and stop controls`.

---

### Task 10: Docs, single-source version, CI reconciled with the code

**Why (review findings, docs drift in both reports):** README/CLAUDE.md overstate barge-in and `?v=` coverage, describe the superseded inline-base64 TTS design, have two `## 8` sections, hard-coded test counts, duplicated version strings, a changelog that mentions removed key handling as current.

**Files:**
- Modify: `meraki/__init__.py`, `meraki/config.py` (`APP_VERSION` imports `__version__`), `README.md`, `CLAUDE.md`, `.env.example` (document `MERAKI_ALLOWED_ORIGINS`)
- Modify: `.github/workflows/ci.yml` only if Task 4/5 left it inconsistent

**Interfaces:**
- Consumes: the final behaviour from Tasks 1-9.
- Produces: docs that match the code.

- [ ] **Step 1: Version.** `meraki/__init__.py` holds `__version__ = "3.1.0"`; `config.py` does `from . import __version__ as APP_VERSION`-style import so there is one source (keep the name `APP_VERSION` exported from `config`). Add a test `test_version_has_one_source` asserting `config.APP_VERSION == meraki.__version__`.
- [ ] **Step 2: CLAUDE.md.** Update §4 and §5 to reflect: `interrupted` is sent whenever audio may still be playing; echo filter is word-based with a two-word floor and expiry; `stream_speech` delivers as chunks finish; the style fallback is per `(voice, model)` and only on a 400 mentioning `style`; handshake validation and the session-id rule (no `"anonymous"`); the Origin allow-list env var; `--ws-max-size`; security headers and `Cache-Control: no-cache` on `/static` (and that module imports and the worklet are therefore revalidated rather than versioned); the `PlaybackGate` and `support.js` modules; the layout table gains the new files; fix the duplicate `## 8` (renumber open items to 9 and changelog to 10); fix the §7 Murf bullet so it no longer describes inline-base64 `generate`; add a changelog entry dated 2026-10-01 summarising this work with the real test counts from running the suites; remove the "TemplateResponse deprecation" inaccuracy; keep the file otherwise intact.
- [ ] **Step 3: README.md.** Correct the barge-in and pipelining claims to match behaviour, update the test counts and the "Layout" tree (new files), mention `MERAKI_ALLOWED_ORIGINS`, and note `requirements-dev.txt` for running tests. Do not claim anything unmeasured: leave the "Measured" table exactly as it is.
- [ ] **Step 4: Run both suites; run `python -c "import meraki.main"`; boot the app and hit `/health` and `/` (as CI does).** Commit `docs: reconcile README and CLAUDE.md with the behaviour; single-source the version`.
