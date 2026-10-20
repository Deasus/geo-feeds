#!/usr/bin/env python3
"""Run every feed on its own cadence inside ONE long-lived job, publishing each
feed's data/ directory as a single-commit branch.

Why one job: GitHub's `schedule` trigger is unreliable at short intervals, and a
self-re-dispatching chain per feed would hold one runner per feed. Here one runner
loops for ~5h20m, runs whichever feeds are due (fast and slow lanes in parallel),
then exits; the workflow has already queued its successor.

Why single-commit branches: every publish is one parentless commit, force-pushed
to the feed's branch. Clients always read the latest file and the repository never
accumulates data history, so it stays small.

For each feed: its code is copied into work/<feed>/, its previous output is
restored from its branch into work/<feed>/data/, it runs there, and data/ is
published if its tree changed. status.json on the `status` branch records every
feed's health.

Env: CONDUCTOR_ONLY=a,b  run just these feeds once and exit (debugging)
     CONDUCTOR_PUSH=0    never push (local dry runs)
     CONDUCTOR_RUN_SECONDS, CONDUCTOR_FAST_WORKERS, CONDUCTOR_SLOW_WORKERS
"""
import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from feeds import FEEDS

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, 'work')
RUN_SECONDS = int(os.environ.get('CONDUCTOR_RUN_SECONDS', str(5 * 3600 + 20 * 60)))
ONLY = [f for f in os.environ.get('CONDUCTOR_ONLY', '').replace(' ', '').split(',') if f]
PUSH = os.environ.get('CONDUCTOR_PUSH', '1') == '1'
FAST_EVERY = 300                      # feeds this frequent run in the fast lane
STATUS_EVERY = 300
BOT = {'GIT_AUTHOR_NAME': 'geo-feeds', 'GIT_AUTHOR_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com',
       'GIT_COMMITTER_NAME': 'geo-feeds', 'GIT_COMMITTER_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com'}

push_lock = threading.Lock()
state_lock = threading.Lock()
status = {'feeds': {}}
trees = {}                            # feed -> last published tree id


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log(msg: str) -> None:
    print(f'{now_iso()} {msg}', flush=True)


def git(*args, env=None, check=True) -> str:
    e = dict(os.environ, **BOT)
    e.update(env or {})
    r = subprocess.run(['git', *args], cwd=ROOT, env=e, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f'git {" ".join(args[:2])} failed: {r.stderr.strip()[:300]}')
    return r.stdout.strip()


def with_index(fn):
    """Run fn(env) with a throwaway index so the main checkout is never touched."""
    fd, idx = tempfile.mkstemp(prefix='geofeeds-idx-')
    os.close(fd)
    os.unlink(idx)
    try:
        return fn({'GIT_INDEX_FILE': idx})
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def restore(branch: str, dest: str):
    """Fetch the branch (depth 1) and check its tree out into dest. Returns its tree id or None."""
    os.makedirs(dest, exist_ok=True)
    ref = f'refs/remotes/origin/{branch}'
    r = subprocess.run(['git', 'fetch', '--quiet', '--depth', '1', 'origin', f'+refs/heads/{branch}:{ref}'],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    with_index(lambda env: git(f'--work-tree={dest}', 'checkout', ref, '--', '.', env=env))
    return git('rev-parse', f'{ref}^{{tree}}')


def publish(branch: str, src: str, ignore=(), force=False) -> bool:
    """Publish src/ as one parentless commit on branch if its tree changed."""
    if not os.path.isdir(src) or not os.listdir(src):
        return False
    if ignore:
        with open(os.path.join(src, '.gitignore'), 'w') as f:
            f.write('\n'.join(ignore) + '\n')

    def build(env):
        git(f'--work-tree={src}', 'add', '-A', '.', env=env)
        return git('write-tree', env=env)
    tree = with_index(build)
    if tree == trees.get(branch) and not force:
        return False
    commit = git('commit-tree', tree, '-m', f'{branch} {now_iso()}')
    if PUSH:
        with push_lock:
            for attempt in range(4):
                r = subprocess.run(['git', 'push', '--force', '--quiet', 'origin', f'{commit}:refs/heads/{branch}'],
                                   cwd=ROOT, capture_output=True, text=True)
                if r.returncode == 0:
                    break
                time.sleep(5 * (attempt + 1))
            else:
                raise RuntimeError(f'push {branch} failed: {r.stderr.strip()[:200]}')
    trees[branch] = tree
    return True


def prepare(feed: str, spec: dict) -> str:
    wd = os.path.join(WORK, feed)
    os.makedirs(os.path.join(wd, 'data'), exist_ok=True)
    for code_dir in spec['code']:
        src = os.path.join(ROOT, 'feeds', code_dir)
        for name in os.listdir(src):
            if name.endswith('.py'):
                shutil.copy2(os.path.join(src, name), os.path.join(wd, name))
    return wd


def run_feed(feed: str, spec: dict) -> None:
    st = status['feeds'].setdefault(feed, {})
    started = time.time()
    with state_lock:
        st.update(last_start=now_iso(), last_start_ts=started, running=True)
    wd = os.path.join(WORK, feed)
    env = dict(os.environ, PYTHONUNBUFFERED='1', **spec.get('env', {}))
    ok, tail = True, ''
    for cmd in spec['run']:
        try:
            r = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True, timeout=spec['timeout'])
            out = (r.stdout or '') + (r.stderr or '')
            tail = out.strip()[-600:]
            if r.returncode != 0:
                ok = False
                log(f'[{feed}] {" ".join(cmd[1:])} exited {r.returncode}: {tail[-300:]}')
                if not spec.get('keep_going'):
                    break
        except subprocess.TimeoutExpired:
            ok, tail = False, f'timeout after {spec["timeout"]}s'
            log(f'[{feed}] {" ".join(cmd[1:])} timed out')
            if not spec.get('keep_going'):
                break
    try:
        published = publish(feed, os.path.join(wd, 'data'), spec.get('ignore', ()))
        err = None
    except Exception as e:
        published, err = False, str(e)[:300]
        log(f'[{feed}] publish failed: {err}')
    dur = round(time.time() - started, 1)
    with state_lock:
        st.update(running=False, last_end=now_iso(), duration_s=dur, ok=ok and err is None,
                  last_tail=tail[-300:] if not ok else '', publish_error=err)
        st['runs'] = st.get('runs', 0) + 1
        if ok:
            st['last_ok'] = now_iso()
        else:
            st['failures'] = st.get('failures', 0) + 1
        if published:
            st['last_published'] = now_iso()
    log(f'[{feed}] {"ok" if ok else "FAILED"} in {dur}s{" · published" if published else ""}')


def write_status(force=False) -> None:
    sd = os.path.join(WORK, '_status')
    os.makedirs(sd, exist_ok=True)
    with state_lock:
        status['generated_at'] = now_iso()
        snapshot = json.dumps(status, indent=1, sort_keys=True)
    with open(os.path.join(sd, 'status.json'), 'w') as f:
        f.write(snapshot)
    try:
        publish('status', sd, force=force)
    except Exception as e:
        log(f'[status] publish failed: {e}')


def due(feed: str, spec: dict, t: float) -> bool:
    st = status['feeds'].get(feed, {})
    return not st.get('running') and t - st.get('last_start_ts', 0) >= spec['every']


def main() -> int:
    feeds = {k: v for k, v in FEEDS.items() if not ONLY or k in ONLY}
    restored = restore('status', os.path.join(WORK, '_status'))
    if restored:
        try:
            with open(os.path.join(WORK, '_status', 'status.json')) as f:
                status.update(json.load(f))
            trees['status'] = restored
        except (OSError, ValueError):
            pass
    for st in status['feeds'].values():
        st['running'] = False

    active = {}
    for feed, spec in feeds.items():
        missing = [v for v in spec.get('requires', []) if not os.environ.get(v)]
        if missing:
            status['feeds'].setdefault(feed, {}).update(skipped=f'missing {", ".join(missing)}')
            log(f'[{feed}] skipped: missing {", ".join(missing)}')
            continue
        status['feeds'].setdefault(feed, {}).pop('skipped', None)
        wd = prepare(feed, spec)
        tree = restore(feed, os.path.join(wd, 'data'))
        if tree:
            trees[feed] = tree
        active[feed] = spec
    log(f'conductor: {len(active)} feeds active, push={"on" if PUSH else "off"}, '
        f'{"once" if ONLY else f"for {RUN_SECONDS}s"}')

    if ONLY:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for feed, spec in active.items():
                pool.submit(run_feed, feed, spec)
        write_status(force=True)
        return 0

    fast = ThreadPoolExecutor(max_workers=int(os.environ.get('CONDUCTOR_FAST_WORKERS', '4')))
    slow_n = int(os.environ.get('CONDUCTOR_SLOW_WORKERS', '3'))
    slow = ThreadPoolExecutor(max_workers=slow_n)
    slow_running = set()
    deadline = time.time() + RUN_SECONDS
    last_status = 0.0
    while time.time() < deadline:
        t = time.time()
        for feed in sorted(active, key=lambda f: active[f]['every']):   # most frequent first
            spec = active[feed]
            if not due(feed, spec, t):
                continue
            if spec['every'] <= FAST_EVERY:
                status['feeds'].setdefault(feed, {})['running'] = True
                fast.submit(run_feed, feed, spec)
            elif len(slow_running) < slow_n:
                status['feeds'].setdefault(feed, {})['running'] = True
                slow_running.add(feed)
                slow.submit(run_feed, feed, spec).add_done_callback(lambda _f, f=feed: slow_running.discard(f))
        if t - last_status >= STATUS_EVERY:
            write_status()
            last_status = t
        time.sleep(5)
    log('conductor: deadline reached; waiting for running feeds')
    fast.shutdown(wait=True)
    slow.shutdown(wait=True)
    write_status(force=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
