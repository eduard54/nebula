import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nebula.core.aggregation.updatehandlers.updatehandler import UpdateHandler
from nebula.core.eventmanager import EventManager
from nebula.core.nebulaevents import NodeTerminatedEvent, UpdateNeighborEvent, UpdateReceivedEvent
from nebula.core.utils.locker import Locker

if TYPE_CHECKING:
    from nebula.core.aggregation.aggregator import Aggregator


class Update:
    def __init__(self, model, weight, source, round):
        self.model = model
        self.weight = weight
        self.source = source
        self.round = round


class CAFFUpdateHandler(UpdateHandler):
    def __init__(self, aggregator: "Aggregator", addr: str):
        self._addr = addr
        self._aggregator = aggregator

        self._update_cache: dict[str, Update] = {}
        self._terminated_peers: set[str] = set()
        self._sources_expected: set[str] = set()

        self._cache_lock = Locker(name="caff_cache_lock", async_lock=True)

        self._fallback_task = None
        self._notification_sent = False

        self._aggregation_fraction = 1.0
        self._staleness_threshold = 1

        self._max_local_rounds = 1

        self._local_round = 0
        self._K_node = 1

        self._local_update: Update | None = None
        self._local_training_done = False

        self._should_stop_training = False

    @property
    def us(self):
        return self._update_cache

    @property
    def agg(self):
        return self._aggregator

    async def init(self, config):
        self._aggregation_fraction = config.participant["caff_args"]["aggregation_fraction"]
        self._staleness_threshold = config.participant["caff_args"]["staleness_threshold"]
        # self._fallback_timeout = config.participant["caff_args"]["fallback_timeout"]
        self._max_local_rounds = config.participant["scenario_args"]["rounds"]

        await EventManager.get_instance().subscribe_node_event(UpdateNeighborEvent, self.notify_federation_update)
        await EventManager.get_instance().subscribe_node_event(UpdateReceivedEvent, self.storage_update)
        await EventManager.get_instance().subscribe_node_event(NodeTerminatedEvent, self.handle_node_termination)
        await EventManager.get_instance().subscribe(("control", "terminated"), self.handle_peer_terminated)

    async def handle_peer_terminated(self, source: str, message: Any):
        # 1) Guard against repeated termination handling
        if self._should_stop_training:
            logging.info(f"[CAFF] DEBUG should_stop_training = {self._should_stop_training}-----------------")
            return

        logging.info(f"[CAFF] Received TERMINATED control message from {source}")
        self._terminated_peers.add(source)
        self._sources_expected.discard(source)
        await self._check_k_node_satisfaction()

    async def _check_k_node_satisfaction(self):
        # 2) If we’re already stopping, skip any further checks
        if self._should_stop_training:
            return

        active_peers_remaining = len(self._sources_expected - {self._addr} - self._terminated_peers)
        required_external = self._K_node - 1
        logging.info(
            f"[CAFF] Checking K_node feasibility: active_peers={active_peers_remaining}, required_external={required_external}"
        )

        if active_peers_remaining < required_external:
            logging.warning("[CAFF] Not enough active peers to reach K_node. Triggering self-termination.")
            # flip the stop flag so everyone knows we’re shutting down
            self._should_stop_training = True
            await self.terminate_self()

            # also let the engine know immediately
            if hasattr(self._aggregator, "engine"):
                logging.info("[CAFF] STOP: Notifying Engine to stop training")
                self._aggregator.engine.force_stop_training()

    async def round_expected_updates(self, federation_nodes: set):
        self._sources_expected = federation_nodes.copy()
        # self._terminated_peers.clear()
        self._update_cache.clear()
        self._notification_sent = False
        self._local_round = self._aggregator.engine.get_round()

        # Exclude self from expected peers, then add +1 later in aggregation threshold
        expected_peers = self._sources_expected - {self._addr}
        self._K_node = max(
            1 + 1, round(self._aggregation_fraction * len(expected_peers)) + 1
        )  # <-- fix here with local update once min reached

        self._local_update = None
        self._local_training_done = False

        # if self._fallback_task:
        #    logging.info(f"[CAFF] FALLBACK TASK TRUE)")
        #    self._fallback_task.cancel()
        # self._fallback_task = asyncio.create_task(self._start_fallback_timer())

    async def storage_update(self, updt_received_event: UpdateReceivedEvent):
        model, weight, source, round_received, local_flag = await updt_received_event.get_event_data()
        await self._cache_lock.acquire_async()

        if self._addr == source:
            update = Update(model, weight, source, round_received)
            self._local_update = update
            self._update_cache[source] = update  # Make sure own update is in cache
            self._local_training_done = True
            logging.info(f"[CAFF] Received own update from {source} (local training done)")
            logging.info(f"[CAFF] [CACHE-AFTER] Update cache for round {self._local_round}: {self._update_cache}")
            await self._cache_lock.release_async()
            await self._maybe_aggregate(force_check=True)
            return

        if source in self._update_cache:
            cached_update = self._update_cache[source]
            if round_received > cached_update.round:
                self._update_cache[source] = Update(model, weight, source, round_received)
                logging.info(f"[CAFF] Replaced cached update from {source} with newer round {round_received}")
            else:
                logging.info(f"[CAFF] Discarded stale update from {source} (round {round_received})")
            await self._cache_lock.release_async()
            if self._local_training_done:
                await self._maybe_aggregate()
            return

        # if source in self._terminated_peers:
        #    logging.info(f"[CAFF] Ignoring update from terminated peer {source}")
        #    await self._cache_lock.release_async()
        #    return

        if self._local_round - round_received <= self._staleness_threshold:
            self._update_cache[source] = Update(model, weight, source, round_received)
            logging.info(f"[CAFF] Cached new update from {source} (round {round_received})")
        else:
            logging.info(f"[CAFF] Discarded stale update from {source} (round {round_received})")

        peer_count = len(self._update_cache)
        has_local = self._local_training_done

        logging.info(
            f"[CAFF] Round {self._local_round} aggregation check: {peer_count} / {self._K_node} (local ready: {has_local})"
        )

        await self._cache_lock.release_async()

        # Trigger aggregate only if local training is complete
        if self._local_training_done:
            await self._maybe_aggregate()

    async def get_round_updates(self):
        await self._cache_lock.acquire_async()
        updates = {peer: (u.model, u.weight) for peer, u in self._update_cache.items()}
        if self._local_update:
            updates[self._addr] = (self._local_update.model, self._local_update.weight)
        logging.info(f"[CAFF] DEBUG local update = {self._local_update}-----------------")
        await self._cache_lock.release_async()
        return updates

    async def notify_federation_update(self, updt_nei_event: UpdateNeighborEvent):
        source, remove = await updt_nei_event.get_event_data()
        if remove:
            self._terminated_peers.add(source)
            logging.info(f"[CAFF] Peer {source} marked as terminated")

    async def get_round_missing_nodes(self):
        return self._sources_expected.difference(self._update_cache.keys()).difference(self._terminated_peers)

    async def notify_if_all_updates_received(self):
        logging.info("[CAFF] DEBUG NOTIFY ALL UPDATES RECEIVED---------------")
        await self._maybe_aggregate()

    async def stop_notifying_updates(self):
        if self._fallback_task:
            self._fallback_task.cancel()
            self._fallback_task = None

    # async def mark_self_terminated(self):
    #    source_id = self._aggregator.get_id()
    #    await EventManager.get_instance().publish_node_event(NodeTerminatedEvent(source_id))

    async def handle_node_termination(self, event: NodeTerminatedEvent):
        source = await event.get_event_data()
        if source in self._terminated_peers:
            return
        self._terminated_peers.add(source)
        logging.info(f"[CAFF] Peer {source} terminated, remaining expected: {self._sources_expected}")

        # re-evaluate K_node feasibility immediately
        await self._check_k_node_satisfaction()

        # if source not in self._terminated_peers:
        #    self._terminated_peers.add(source)
        #    #self._sources_expected.discard(source)
        #    logging.info(f"[CAFF] Node {source} terminated")
        #    logging.info(f"[CAFF] Updated terminated peers: {self._terminated_peers}")
        #    logging.info(f"[CAFF] Remaining active peers: {self._sources_expected - {self._addr} - self._terminated_peers}")
        #    if not self._local_training_done:
        #        await self. (force_check=True)
        #    else:
        #        logging.info("[CAFF] Ignoring termination impact – local training already done.")

    async def _maybe_aggregate(self, force_check=False):
        logging.info(
            f"[{self._addr}] ENTERED _maybe_aggregate (round={self._local_round}) | local_done={self._local_training_done} | cache={list(self._update_cache.keys())}"
        )
        await self._cache_lock.acquire_async()

        if self._notification_sent and not force_check:
            logging.info("CAFF DEBUGG got out of notification send")
            await self._cache_lock.release_async()
            return

        # if self._addr in self._update_cache:
        #    logging.info(f"----------------[CAFF] LOCAL UPDATE IS IN CACHE -------------------------")
        #    self._local_training_done = True

        peer_count = len(self._update_cache)
        has_local = self._local_training_done

        logging.info(
            f"[CAFF] Round {self._local_round} aggregation check: {peer_count} / {self._K_node} (local ready: {has_local})"
        )

        if self._addr in self._update_cache:
            logging.info(f"[CAFF] [MAYBE_AGGREGATE] Local update is already cached for round {self._local_round}")
        else:
            logging.warning(
                f"[CAFF] [MAYBE_AGGREGATE] Local update is NOT cached at aggregation time (round {self._local_round})"
            )

        if not has_local:
            logging.debug("[CAFF] Waiting for local training to finish before aggregation.")
            await self._cache_lock.release_async()
            return

            # ✅ NEW: For round 0, enforce synchronous wait for all expected sources
        if self._local_round == 0:
            expected = self._sources_expected | {self._addr}
            logging.info(f"Expected: {expected}")
            received = set(self._update_cache.keys())
            logging.info(f"Received: {received}")
            missing = expected - received
            logging.info(f"Missing: {missing}")
            if missing:
                logging.info(f"[CAFF][ROUND 0 SYNC] Waiting for all updates in round 0. Still missing: {missing}")
                await self._cache_lock.release_async()
                return
            else:
                logging.info("[CAFF][ROUND 0 SYNC] All updates received for round 0 — proceeding with aggregation")

        if has_local and peer_count >= self._K_node:
            logging.info(f"[CAFF] Aggregating with {peer_count} updates (incl. local)")
            self._notification_sent = True
            await self._cache_lock.release_async()
            await asyncio.sleep(0.5)
            await self.agg.notify_all_updates_received()
            return

        active_peers_remaining = len(self._sources_expected - {self._addr} - self._terminated_peers)
        required_external = self._K_node - 1

        if active_peers_remaining < required_external:
            logging.warning("[CAFF] Not enough active peers to reach K_node. Triggering self-termination.")
            self._should_stop_training = True
            logging.warning(
                f"[CAFF] Triggered self-termination due to unsatisfiable K_node condition (active: {active_peers_remaining}) | (required: {required_external})"
            )
            await self.terminate_self()
            if hasattr(self._aggregator, "engine"):
                self._aggregator.engine.force_stop_training()
            return

        await self._cache_lock.release_async()

    # async def _start_fallback_timer(self):
    #     await asyncio.sleep(self._fallback_timeout)
    #     await self._cache_lock.acquire_async()
    #     if self._notification_sent:
    #         await self._cache_lock.release_async()
    #         return
    #     if self._update_cache and self._local_training_done:
    #         logging.warning("[CAFF] Fallback triggered — aggregating with partial cache.")
    #         self._notification_sent = True
    #         await self._cache_lock.release_async()
    #         await self.agg.notify_all_updates_received()
    #     else:
    #         await self._cache_lock.release_async()

    async def should_continue_training(self) -> bool:
        logging.info(
            f"[CAFF] should_continue_training = {not self._should_stop_training} (should_stop_training: {self._should_stop_training})"
        )
        return not self._should_stop_training

    async def terminate_self(self):
        # 3) Idempotency: only run this block once
        if self._notification_sent:
            return

        # mark that we've broadcast our termination
        self._notification_sent = True
        self._should_stop_training = True

        logging.warning(f"[CAFF] Node {self._addr} terminating itself.")

        # cancel the fallback timer if it’s still pending
        # if self._fallback_task:
        #     self._fallback_task.cancel()
        #     self._fallback_task = None

        # publish our own termination event
        await EventManager.get_instance().publish_node_event(NodeTerminatedEvent(self._addr))

        # send the control‐terminated message exactly once
        terminate_msg = self._aggregator.engine.cm.create_message("control", "terminated")
        await self._aggregator.engine.cm.send_message_to_neighbors(terminate_msg)

        # also flip the engine’s flag so the loop exits ASAP
        if hasattr(self._aggregator, "engine"):
            self._aggregator.engine.force_stop_training()
