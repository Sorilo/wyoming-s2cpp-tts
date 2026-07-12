/goal

Proceed with Phase 9.5 only: design and implement progressive phrase synthesis for streaming Wyoming TTS input.

Project:
/workspace/wyoming-s2cpp-tts

Repository baseline:

* Start from merged main at:
    d442b930863d3a9929ecc36e106a503ffa31d796
* Confirm local main, origin/main, and the clean worktree before changing anything.
* Inspect the Phase 9B scheduler domain, Phase 9C lifecycle/coordinator implementation, app/wyoming_server.py, app/speech/, all Wyoming streaming tests, and the current roadmap.
* Produce and critically review a written implementation plan before modifying production code.

Primary objective:
Allow synthesis of a streaming LLM response to begin at deterministic phrase boundaries before the entire textual response has arrived, while preserving one logical Wyoming synthesis response and all existing scheduler, cancellation, privacy, timeout, and shutdown guarantees.

Architecture requirements:

* Implement incremental phrase parsing as a dedicated, independently tested component rather than embedding phrase-boundary logic directly throughout app/wyoming_server.py.
* Keep SpeechScheduler as the sole owner of synthesis admission, FIFO ordering, active synthesis identity, cancellation, timeout, release, and terminal accounting.
* Serialize every emitted phrase through the existing SpeechScheduler.
* Preserve exactly one active s2.cpp synthesis operation at a time.
* Do not add a second scheduler, bypass path, direct backend invocation path, or hidden per-connection worker queue.
* Treat the entire streamed text response as one logical Wyoming request, even though it may create multiple sequential backend synthesis operations.
* Clearly document which state belongs to the logical request and which state belongs to an individual phrase synthesis.

Phrase-boundary contract:

* Define the complete deterministic phrase-boundary algorithm before implementation.
* At minimum, support terminal sentence punctuation and bounded fallback behavior for text streams that contain no terminal punctuation.
* Avoid splitting inside common abbreviations, decimal numbers, ellipses, and other explicitly documented protected cases where practical.
* Preserve whitespace and punctuation sufficiently for natural TTS pronunciation.
* Do not emit empty or whitespace-only phrases.
* Flush all remaining buffered text on synthesize-stop.
* Apply explicit configurable or constant bounds for:
    * maximum buffered characters,
    * maximum phrase characters,
    * and any fallback soft-boundary threshold.
* A punctuation-free or malformed stream must not cause unbounded memory growth.
* Do not add semantic rewriting, summarization, priority, replacement, deduplication, or request coalescing.

Wyoming protocol compatibility:

* Support streaming synthesize-start, synthesize-chunk, and synthesize-stop input.
* Preserve compatibility with the existing full-message Synthesize event.
* Prevent the compatibility full-message Synthesize event from synthesizing text that was already accepted or synthesized through streaming chunks.
* Deterministically handle:
    1. streaming events only,
    2. legacy full-message Synthesize only,
    3. streaming sequence followed by a compatibility full-message event,
    4. compatibility full-message event followed by or interleaved with streaming events,
    5. duplicate stop or terminal events,
    6. disconnect before stop,
    7. empty streaming input.
* Do not identify duplicates by storing or exposing plaintext in logs, metrics, reprs, admin responses, or unbounded labels.
* Document the exact compatibility rule and which event source becomes authoritative for a logical request.

Audio and event continuity:

* Emit one coherent Wyoming audio response for the logical streamed request.
* Preserve continuous audio format metadata across phrases.
* Preserve monotonic and continuous timestamps/sample accounting across phrase boundaries.
* Do not emit a fresh logical response start/end pair for each internal phrase unless the existing Wyoming protocol strictly requires it and tests prove compatibility.
* Never overlap audio from adjacent phrase synthesis operations.
* Preserve phrase ordering exactly.
* Ensure the final audio-stop or equivalent terminal event is emitted exactly once.
* If a later phrase fails after earlier audio has already been emitted, terminate the logical response deterministically using the existing controlled-error contract without replaying or duplicating prior audio.

Cancellation, disconnect, timeout, and drain behavior:

* A disconnect or cancellation must stop:
    * buffered but not yet admitted phrases,
    * queued phrase work,
    * and the currently active phrase synthesis where supported.
* No later phrase may begin after logical-request cancellation.
* Cleanup and scheduler release must remain exactly once.
* Preserve queue-wait, synthesis-timeout, backend-busy retry, and capacity behavior from Phase 9 and Phase 9B.
* Define whether existing timeout budgets apply per phrase or to the entire logical streamed request, justify the decision, and test it.
* When graceful drain begins:
    * reject new logical requests,
    * prevent additional phrase admissions for requests that are no longer allowed to continue,
    * preserve the documented Phase 9C active-work grace behavior,
    * and reach deterministic scheduler quiescence.
* Preserve readiness, lifecycle state, admin HTTP behavior, privacy guarantees, cumulative counters, and terminal accounting.
* Explicitly define whether counters count logical requests or individual internal phrase operations. Public operational counters should remain meaningful and must not silently change semantics.

Testing requirements:

* Add deterministic unit and integration tests using events, futures, barriers, controlled async iterators, and fake backends.
* Do not use arbitrary sleeps as synchronization.
* Test phrase parsing independently, including:
    * normal punctuation,
    * punctuation split across chunks,
    * multiple phrases in one chunk,
    * abbreviations,
    * decimal numbers,
    * ellipses,
    * whitespace-only chunks,
    * Unicode punctuation where supported,
    * long punctuation-free input,
    * maximum-buffer fallback,
    * final residual flush,
    * and exact-once phrase emission.
* Test scheduler serialization and prove that no two phrase backend calls overlap.
* Test continuous audio metadata, sample counts, timestamps, phrase order, and exactly-once terminal events.
* Test cancellation and disconnect:
    * before the first phrase,
    * while a phrase is queued,
    * during active phrase synthesis,
    * between phrases,
    * and after partial audio has been emitted.
* Test queue full, backend busy, queue timeout, synthesis timeout, graceful drain, forced shutdown, and cleanup races.
* Test all legacy/streaming compatibility event orders and prove that no text is synthesized twice.
* Retain all existing Phase 9B and Phase 9C regression tests.

Review and verification:

* Implement in focused, independently reviewable commits.
* Prefer regression-first development for every discovered contract defect.
* Perform a complete-diff review after implementation, including concurrency, protocol event ordering, accounting semantics, privacy, cancellation, shutdown, and backwards compatibility.
* Correct blocking findings in separate commits without rewriting prior implementation commits.
* Run focused phrase, streaming, scheduler, disconnect, timeout, lifecycle, coordinator, admin, metrics, and shutdown test sets.
* Run the authoritative application suite:

    ```bash
    .venv/bin/python -m pytest tests/ \
      --ignore=tests/test_realtime_tuning_unraid.py \
      -q -o addopts=
    ```
* Require zero failures, zero unexpected skips, no leaked-task warnings, no unclosed stream/session warnings, no unobserved task exceptions, and clean git diff checks.
* Invoke the Unraid-specific suite separately and report its result without treating host-environment timeouts as authoritative application failures.
* Correct stale Phase 9C documentation references from 1,112 passed to the final reviewed baseline of 1,113 passed where applicable.

Documentation:
Update:

* README.md
* docs/ARCHITECTURE.md
* docs/ROADMAP.md
* TODO.md
* CHANGELOG.md
* docs/NEXT_GOAL_PROMPTS.md

Document:

* phrase-boundary algorithm,
* buffering limits,
* legacy versus streaming authority rules,
* logical-request versus phrase-operation ownership,
* timeout semantics,
* cancellation behavior,
* drain behavior,
* audio continuity,
* accounting semantics,
* known latency and prosody tradeoffs,
* and remaining limitations.

Do not:

* Implement Phase 10 barge-in, VAD-triggered cancellation, or playback interruption.
* Add semantic priority, phrase replacement, speculative synthesis, deduplication, request coalescing, multi-worker scheduling, concurrent backend synthesis, or multi-GPU scheduling.
* Change Home Assistant, Unraid templates, production containers, backend services, models, quantization, voices, or voice-cloning behavior.
* Build, publish, tag, or deploy container images unless separately authorized.
* Expose synthesis plaintext, identifiers, request paths, raw audio, secrets, environment contents, or unbounded metric labels.
* Modify the currently deployed production state.

Production must remain on:

* Wrapper:
    ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7
* Backend:
    ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-6e629d0

At completion, report:

* initial repository state,
* reviewed architecture and phrase-boundary contract,
* complete-diff review findings,
* exact commits and SHAs,
* focused and authoritative test commands and counts,
* streaming and legacy compatibility behavior,
* timeout and accounting semantics,
* cancellation and graceful-drain behavior,
* audio continuity verification,
* known limitations,
* confirmation that production and images were untouched,
* and the exact recommended next-phase /goal.

Do not merge, publish, or deploy unless separately authorized.

---

# Critical Review Decision Record

The implementation review compared the goal with the live Phase 9B/9C code and three independent reviews. The following decisions are binding for implementation.

## Accepted architecture

1. Add a pure bounded `PhraseAccumulator` and a separate logical audio-envelope component.
2. Use one explicit connection-owned streaming coordinator task so Wyoming event ingestion can continue while a phrase synthesizes. Its handoff is bounded and session-local; it is not an admission or scheduling queue.
3. Submit exactly one phrase at a time through `SpeechScheduler.run()` and await completion before submitting the next. No backend calls overlap.
4. Keep existing global FIFO behavior. Do not add logical-request scheduler affinity, scheduler grouping, slot retention, or atomic multi-phrase ownership. Phrase order within each logical request is preserved because that request never submits phrase N+1 before N completes; other connections may fairly run between phrases.
5. Do not modify `SpeechScheduler` unless a regression-first public-boundary test proves an existing API is insufficient.

## Parsing decisions

- Initial limits: soft fallback 160, maximum phrase 320, retained buffer 640 characters. Tests may prove these need adjustment before production wiring.
- Feed chunks verbatim; never insert spaces. Internal whitespace is preserved. Whitespace consumed only between an emitted terminal phrase and the next non-whitespace token is transport boundary whitespace; exactness tests compare all non-boundary content and prove chunking invariance.
- Terminal set starts conservatively with `. ! ? 。 ！ ？`.
- Decimal periods require digits on both sides and are protected.
- Use a finite case-insensitive abbreviation set, documented in code and tests. Avoid an expansive address/month/credential list until a failing requirement justifies it.
- Ellipsis runs remain attached to preceding text and are not split internally. Their final boundary behavior is deterministic and tested across chunk splits.
- Fallback order: confirmed sentence boundary, then last whitespace at/before soft threshold, then last whitespace at/before maximum phrase, then hard split at maximum phrase.
- Buffer enforcement repeatedly emits bounded fallback phrases; a single feed cannot leave retained state above the maximum.
- `flush()` emits non-whitespace residual exactly once; whitespace-only residual emits nothing.
- Add missing degeneracy tests: bare punctuation, decimal at EOF, abbreviation plus sentence, multipart abbreviation, mixed CJK/ASCII, empty chunks, overlong single token, and every supported case under multiple chunkings.

## Compatibility decisions

- Authority is scoped to the current active streaming session, not recent connection history.
- A completed standalone legacy request and a later streaming request are independent, even if their text happens to match; valid repeated speech must not be fingerprint-suppressed.
- Once any non-whitespace streaming chunk is accepted, streaming is authoritative and compatibility text is ignored as synthesis input.
- If no non-whitespace chunk was accepted, deferred compatibility text is used once on stop.
- No overlap comparison, prefix matching, or plaintext history is introduced.
- Duplicate start returns controlled `stream_already_active` and preserves the original active session; this must first be encoded as a failing test.
- Duplicate stop and orphan chunk/stop remain no-ops.

## Timeout, counters, cancellation, and drain

- Existing queue-wait and synthesis deadlines apply per phrase operation. No new total-stream or inter-phrase timeout is added in Phase 9.5; that would be new policy beyond preserving current budgets.
- Existing cumulative counters continue to count scheduler operations. For progressive streaming these are phrase operations. Documentation will make that explicit rather than silently relabeling fields.
- Disconnect/cancellation atomically marks the logical session closed, clears parser and pending handoff state, prevents future admissions, cancels active/waiting connection work through existing scheduler APIs, closes the active generator exactly once, and awaits coordinator termination.
- Drain allows the already admitted phrase the Phase 9C grace behavior. The coordinator checks lifecycle/scheduler acceptance before every later admission; remaining buffered/pending phrases are discarded and no later backend call starts.
- Forced shutdown uses existing scheduler cancellation/quiescence plus handler coordinator cleanup. Tests must prove no coordinator task or generator leaks.

## Audio and terminal-event decisions

- First internal `AudioStart` locks rate/width/channels and is forwarded once. Later starts are suppressed after exact format validation.
- Internal phrase `AudioStop` events are suppressed.
- Chunk timestamps are rebuilt from cumulative emitted PCM frames: `floor(prior_frames * 1000 / rate)`.
- Frame alignment is mandatory; format drift or misalignment is a controlled failure.
- On success, emit one logical `AudioStop` at cumulative frame time, then one `SynthesizeStopped`.
- On failure after partial audio, emit one envelope-closing `AudioStop` at cumulative frame time, then the existing controlled Wyoming `Error`; do not emit `SynthesizeStopped` and do not replay prior audio. This `AudioStop` closes framing and does not indicate backend success.
- On failure before `AudioStart`, emit only the controlled error.
- Tests drive the real handler and assert successful writes, phrase/backend ordering, exact terminal counts, and format/timestamp continuity using events/futures/barriers rather than arbitrary sleeps.

## Planned implementation slices

1. Bounded phrase parser and exhaustive chunking-invariance tests.
2. Logical audio-envelope normalizer and frame-accounting tests.
3. Explicit streaming coordinator and real-handler progressive-before-stop tests.
4. Legacy/streaming authority and duplicate-event order tests.
5. Cancellation, capacity, timeout, backend-busy, drain, forced-stop, and cleanup-race tests.
6. Documentation/baseline corrections, complete-diff review, focused and authoritative verification.

Each production behavior follows RED-GREEN-REFACTOR. Each slice is a separate focused commit. Blocking complete-diff findings receive separate regression-first commits without rewriting slice history. No merge, image, publication, deployment, or production change is authorized.
