# Bounded HAL control publication correction

Status: adopted controller contract before real registration or capture, based on the independent review of `hal-publication-design-1793293537833248000/OwnedHalCore.h`. The review itself ran no tests or device actions. This correction preserves all existing audio, lease, responsiveness and qualification bounds.

## Concrete defect

`ControlMailbox::Publish` marks the shared generation odd before writing atomic payload words. A preempted writer makes every callback's `Read` fail and `Render` call `CommitZero`, even though the previous control and lease remain valid. Independently, a successful unchanged renewal between `Read` and `Acquire` changes that same generation and also causes `CommitZero`. Neither silence is a user pause or an expired lease. Atomic payload words avoid a data race but do not solve availability.

**Replace this with one callback-owned current snapshot and one capacity-one SPSC pending slot. Ordinary publication must never invalidate acquisition of the current snapshot.** Keep native safety fences independent of this transport.

## Ownership, capacity and interfaces

- One serialized non-real-time owner is the only producer. The sole, non-overlapping IOProc is the consumer. If that ownership cannot be maintained, this design is unsupported.
- Preallocate `pending_payload`, atomic monotone `published`/`consumed` counters, and callback-owned `current`, `have_current`, last applied transport/revision state and native resume-grant state. Callback temporary copies have fixed size. No allocation, reference-count operation, lock or wait occurs in the callback.
- Verify both counters, the native gate and tracking-intent word are lock-free before registration. Reject counter exhaustion; do not wrap or reuse sequence identities.
- Suggested interfaces: `TryOffer(validated_control) -> Queued | Busy | Invalid | Retired`; callback-only `TakePending(out) -> bool`; `NativeSpeed(speed)`; `Retire(reason)`; owner-only `BeginEffectiveIntent(token)`; callback evidence/applied acknowledgement. `Queued` means transport committed, **not applied**.
- `TryOffer` first checks `published == consumed` with the consumer counter acquired. If occupied, return `Busy` **without writing anything**. If empty, fill the non-atomic payload completely, then release-store the next published counter. A writer stalled before that store has published nothing.
- `TakePending` makes one acquire-load of `published`. If one item exists, copy the entire immutable slot into callback-local storage, then release-store the consumed counter. Only after that release may the producer overwrite the slot. No payload pointer survives the copy. If nothing exists, use `current`; do not retry and do not emit silence because of transport activity.
- The producer's acquire of `consumed` prevents overwrite while the consumer copies. The consumer's acquire of `published` makes the completed payload visible. This is the same bounded SPSC ownership proof as the PCM/evidence queues, with capacity one. Non-atomic payload accesses are race-free because ownership, not an optimistic version check, excludes concurrent access.
- At most one offer is consumed per callback. A publication arriving after the callback sampled `published` remains pending until the next callback. There is no unbounded drain loop.

`Busy` is normal backpressure, not a session failure. The owner retains that exact validated command and retries on its existing non-real-time iterations; it must not advance applied/accepted bookkeeping, overwrite the slot, skip a sequential gain revision, fabricate an acknowledgement or extend a lease. Stop taking additional commands beyond one owner-held offer until it can enqueue. A later command remains in the existing sequential command transport. Cancellation/retirement bypasses this queue. Genuine stale/malformed commands retain the specified rejection/retirement behavior; a full slot alone does not imply either. If delays exhaust the active lease, the callback retires normally. There is no busy grace period.

## Callback adoption and linearization

The callback may select the complete offered snapshot or its existing complete `current`. Validate binding, monotone revision/epoch relationships, derived gain and lease using that one selected snapshot. Never combine a new expiry with old gain/control fields. Compute scheduled end using the unchanged rules. A committed valid renewal is a normal candidate; an unfinished offer cannot refresh the active lease. An already retired session cannot be revived by any later offer.

Separate the current overloaded generation into:

1. A single atomic **native gate** containing irreversible retirement and native speed epoch/state; route, ownership-loss and stop close this gate directly.
2. Publication sequence for transport provenance only.
3. Effective intent/applied epochs for tracking eligibility, described below.

The final native-gate acquisition is one bounded atomic load of the complete gate word, ordered with the native fence mutations (SC is the simplest faithful replacement). This is the successful block's linearization point. All decisions use that observed gate and the selected immutable snapshot. A native pause before this point freezes consumption and emits zeros; route/stop/retirement prevents delivery. A fence after it may allow the already acquired 512-frame block to finish, exactly as already specified. There is **no final comparison against the ordinary publication counter**. A renewal published concurrently cannot cancel a valid block. State-dependent shortfall/failure decisions must use this final gate too, rather than treating a concurrently paused block as a playback shortfall.

After successful acquisition, install the selected snapshot as callback-owned `current`, commit source consumption once, and emit its actual applied identity. If a native fence prevents playing, retain a coherent control snapshot but record the observed fence and effective paused/non-playing state. Taking a slot, installing a requested state and acknowledging an effective resume are distinct facts; do not acknowledge a resume that the native gate rejected.

### Native resume cannot ride an ordinary gain renewal

Native pause/resume events advance a native epoch independently of the writer. A repeated already-normal `Speed(1000)` is an idempotent no-op, matching the existing Authority speed semantics; it cannot demand a fresh resume grant or freeze audio. Every `Speed(0)` establishes a new fence, including repeated pause calls. A paused-to-normal transition advances the epoch, and a deferred playback resume may grant only its eligible exact next epoch: an intervening newer pause/stop/route invalidates it. An unpaused new epoch remains non-playing until an authenticated **new playback transition** grants resume for that exact native epoch. Initial startup has its separately specified initial grant. Preserve the existing stale-command rules.

Represent that grant explicitly, or give `EffectiveControl::native_fence` this strict meaning: it is the token authorized by the playback transition, **not the latest native token copied into every gain refresh**. A gain-only renewal carries the previous grant unchanged. An earlier grant cannot match a later pause/resume epoch. Keep the playback revision granting resume, and reject attempts to attach a fresh grant to an unchanged/stale playback revision. Move applied-resume bookkeeping to the callback/applied-ack path; enqueue success must not update `last_native_resume_revision_` as if it had taken effect.

The callback needs no retrying CAS to clear a global pending flag: its acknowledged grant/epoch and the observed native epoch determine whether playback is permitted. The gate's native state remains authoritative. Evidence publishes both. A newer native epoch invalidates the old acknowledgement immediately even if the writer is stalled halfway through the slot.

## Pending changes and tracking

Pending genuine changes must invalidate **tracking**, not make an otherwise valid current audio snapshot disappear.

The owner publishes a monotone `desired_effective_intent` token when it accepts a real playback/gain/mute transition, before preparing its pending payload. Each snapshot carries the intent token it represents. An unchanged gain renewal increments transport revision but preserves intent token and effective gain epoch. Native fences independently invalidate tracking immediately. Intent-word exhaustion retires; no reuse.

Queries/authorization require the desired intent to match callback-applied effective intent, matching actual effective epochs and native grant/gate, plus all existing timing, lease and acknowledgement-boundary checks. While a real change is pending, source may continue under the old coherent valid snapshot as permitted by the existing control-response contract, but no source observation is newly tracker-authorized under that pending change. Ordinary renewals cause neither audio silence nor acquisition reset. Observations already authorized before the new intent follow the existing queued-authorization recheck.

If an older committed offer is adopted while a still newer intent is pending, retain its real applied fields and acknowledge only that actual adoption; tracking remains pending. Never relabel it as the newer intent. Sequential revision checks prevent an older offer from applying after a newer one was acknowledged. Native pause/stop/route still take priority over either offer.

`HalSegment` and acknowledgements must carry actual publication/transport sequence, playback revision/epoch, gain revision/effective epoch, applied intent, native gate/authorized native epoch and first effective output boundary. Owner reports must distinguish queued versus applied. Retain requested versus effective playing state if needed to make native overrides unambiguous. An ordinary transport-sequence change is provenance, not an output-generation mismatch that rejects otherwise coherent unchanged-gain queries. Query/read-start/end fences compare native safety and effective state/intent, not merely the offer counter.

### Applied state reaches the clock owner through existing evidence ownership

Extend every acquired callback record with a complete, fixed-size copy of the **actually selected/applied control** (or equivalent explicit fields). In particular, the current `HalSegment` is insufficient: add applied request-start, request-end, issue and expiry timestamps, integer volume, initial-start/sample-zero flags, publication sequence, intent and resume grant. Keep actual effective playing/native override separate from the requested `playing` field. These values come from the callback-local selected snapshot, never from the producer's latest desired control. If fields are duplicated in the segment, they must describe that same tuple.

Publish this record through the existing callback-to-owner SPSC evidence ring. The non-real-time clock owner drains records in order and maintains its **own private** last-applied snapshot/sequence/lease from records marked `control_acquired`; a merely queued offer or unacquired zero record cannot update it. No additional callback-to-owner shared plain snapshot or seqlock is necessary. Evidence overflow still retires under the existing rule.

A query uses the covering segment's actual applied tuple to prove that segment and its scheduled lease coverage, and the latest drained acquired tuple for current applied lease/state. An unchanged renewal may advance the latter lease/revision without changing effective epoch, but only after its callback evidence has been consumed. Pending desired expiry never refreshes either view. Revalidate the atomic native gate and desired intent around the query and subsequent tracker/authorization checks. If evidence lag leaves the applied view stale, the query is unqualified; it must not manufacture an acknowledgement from producer state. The audio callback continues independently under its complete current snapshot while its own lease remains valid.

Emit exact applied lease timestamps and record identity in native query/audit output so delayed-drain tests can distinguish queued from applied renewals. Test a committed-but-not-adopted offer and an adopted-but-not-yet-drained record: neither may let the clock owner read callback-local memory or substitute the desired lease. After drain, acknowledgement/query state must match the exact acquired record.

## Lifetime

The pending slot, counters, current snapshot, gate, intent and callback bookkeeping belong to the independent retained HAL context. Neither payload references the addon instance, stack owner storage nor mutable external control objects. Slot reuse is permitted only by the consumed-counter handoff; context reuse/freeing follows the separately settled lifecycle/quarantine rules. Non-real-time readers obtain applied state from the evidence/ack handoff, never by racing a read of callback-local `current`. This correction changes no process/module retention policy.

## Required deterministic checks before capture

These are prescribed tests, not tests executed by this review:

1. Start valid playback, park the writer before the release commit at several payload offsets during an **unchanged renewal**, and run multiple callbacks within the existing lease. Assert exact contiguous source samples/counts, no inserted zero block, unchanged applied revision/expiry and complete evidence. Resume the writer; adoption occurs on a subsequent boundary with correct revision and still no gap/replay.
2. Commit a renewal between the callback's pending-slot sample and final native-gate acquisition. The callback completes its already selected valid snapshot. The next callback adopts the offer. Publication-counter movement must never cause `CommitZero`.
3. Park a genuine gain-change writer after intent publication. Audio continues under the old gain/lease while tracking is pending. After commit, apply the complete new tuple atomically, retain exact old/new boundaries and use the existing reacquisition rule. No hybrid gain/expiry/revision is observed.
4. During either stalled-writer case, deliver native pause before acquisition. The next block is zero with no source advance; release the old renewal and issue native resume. It must not resume playback. Only a new playback transition carrying the current native grant can resume at the next unconsumed frame. Exercise pause/route/stop just before and just after the acquisition point to verify the permitted one-block boundary.
5. Stall beyond the old lease: the callback retires at the existing coverage deadline even if the unpublished payload contains a later expiry. Later writer completion cannot revive it. Test malformed candidates, retired pending offers and unchanged-renewal attempts to change resume grants.
6. Fill the pending slot and call `TryOffer` again. Assert `Busy`, no overwrite, no skipped revision or applied acknowledgement, bounded owner holding, and successful ordered retry after the consumer copy. Park the consumer during its copy: the producer must remain busy until release. Fill every payload field with distinguishable tuples to expose tears or slot reuse.

## Required spec/interface delta

Replace the odd/even multiword mailbox and publication-generation acquisition guard with the capacity-one offer/current design above. State explicitly that release publication queues an offer, callback native-gate acquisition applies it, native safety fencing is independent, and pending genuine changes revoke tracking while unchanged renewals never silence or reset acquisition. Add queued/busy/applied outcomes and actual applied/native-grant provenance. Keep all present lease, startup, gain, callback, EOF, clock and captured decision bounds unchanged. This removes an avoidable publication-induced gap; it neither conceals a shortfall nor makes a capture pass by tolerance adjustment.
