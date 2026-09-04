# -*- coding: utf-8 -*-
"""
PegaProx Health Broadcast - pushes the cluster health rollup over SSE.

Health is a cluster-wide number, so computing it per viewer is waste that grows
with the number of open tabs. This loop computes it once per cluster and hands
it to everyone subscribed to that cluster; the REST endpoint keeps working and
now serves the same cached rollup.

Deliberately NOT part of broadcast_resources_loop(): that runs at 1 Hz, and the
rollup fans out one storage call per online node. It follows background/metrics.py
instead — the expensive collector runs on its own cadence and fills a cache that
cheap consumers read.

Two things keep the cost down:
  * is_cluster_watched() — a cluster nobody is looking at is not computed at all.
  * the ETag from core.health — an unchanged rollup is not re-sent, except for a
    periodic keepalive so a client that connected mid-interval still gets a value.
"""

import logging
import os
import threading
import time

from pegaprox.core.health import (
    get_cluster_health,
    invalidate_cluster_health,
    peek_cluster_health,
)
from pegaprox.globals import cluster_managers
from pegaprox.utils.realtime import broadcast_sse, is_cluster_watched

# Matches the interval the badge used to poll at. That is the point of reference
# that matters: at one viewer this must cost what it cost before, or the change is
# a regression dressed as an optimisation. Every additional viewer is then free.
#
# Keep this BELOW _HEALTH_TTL_S in core.health: this loop is what keeps the cache
# warm, so an entry must not expire between two rounds. Otherwise the REST
# endpoint recomputes from scratch for whoever asks in the gap.
_INTERVAL_S = float(os.environ.get('PEGAPROX_HEALTH_BROADCAST_INTERVAL', '60'))

# Re-send an unchanged rollup every Nth round, so a client that connected after
# the last change still receives a value without waiting for one.
_KEEPALIVE_ROUNDS = int(os.environ.get('PEGAPROX_HEALTH_KEEPALIVE_ROUNDS', '6'))

_thread = None
_running = False
_last_etag = {}     # cluster_id -> etag of the last frame actually sent


def _broadcast_round(round_no):
    for cid, mgr in list(cluster_managers.items()):
        try:
            if not is_cluster_watched(cid):
                # Nobody is subscribed. Drop the remembered etag so a returning
                # viewer gets a frame instead of being deduped against one they
                # never received, and drop the rollup so this cluster stops
                # costing anything at all.
                _last_etag.pop(cid, None)
                invalidate_cluster_health(cid)
                continue
            if not getattr(mgr, 'is_connected', False):
                continue

            # Only keep warm what someone actually asked for. A client subscribes
            # to every cluster it has expanded in the sidebar, but the health
            # badge renders for ONE of them — computing the rest would spend a
            # storage fan-out per cluster on a number nobody displays, which at
            # one viewer is worse than the polling this replaces. The badge's
            # first read goes over REST and fills the cache; from then on this
            # loop keeps it fresh.
            if peek_cluster_health(cid) is None:
                continue

            payload, etag, _from_cache = get_cluster_health(cid, mgr, force=True)
            keepalive = _KEEPALIVE_ROUNDS > 0 and round_no % _KEEPALIVE_ROUNDS == 0
            if etag != _last_etag.get(cid) or keepalive:
                _last_etag[cid] = etag
                broadcast_sse('health', payload, cid)
        except Exception as e:
            # One bad cluster must not stop the others.
            logging.debug(f"[health-broadcast] cluster '{cid}' failed: {e}")


def health_broadcast_loop():
    logging.info("Health broadcast loop started")
    round_no = 0
    while _running:
        round_no += 1
        try:
            _broadcast_round(round_no)
        except Exception as e:
            logging.warning(f"[health-broadcast] round failed: {e}")
        # Sleep in 1s steps so a shutdown is noticed promptly.
        waited = 0.0
        while _running and waited < _INTERVAL_S:
            time.sleep(1)
            waited += 1


def start_health_broadcast_thread():
    """Start the loop once. Idempotent, like start_alert_thread()."""
    global _thread, _running
    if _thread is not None and _thread.is_alive():
        return _thread
    _running = True
    _thread = threading.Thread(target=health_broadcast_loop, daemon=True,
                               name='health-broadcast')
    _thread.start()
    return _thread


def stop_health_broadcast_thread():
    """Ask the loop to finish. Used by tests; nothing stops it in production."""
    global _running
    _running = False
