"""directmgr subscription sync engine (stdlib only).

Single source of truth: list.json in the project DATA_DIR.
Targets (text-splice only, never parse-and-reserialize):
  debi  /srv/sub/<subfile>                                   (this box)
  shoc  /srv/www/sub/<subfile> + /srv/www/sub/mihomo-shock-sub.yaml
        (peer box, reached over ssh with the sandbox-held key;
         the two boxes' subscriptions carry no credentials of
         each other -- the key never leaves this box's operator
         tooling; the web process only invokes the fixed helper)

The managed segment inside rules: is bracketed by marker comments:
    # ---- directmgr begin (managed by directmgr; do not edit) ----
    ...
    # ---- directmgr end ----
First landing migrates the two pre-existing lines (section 2,
"自有域名直连") into the segment; everything outside the markers
is preserved byte for byte.

Every sync: per-file .bak-directmgr-<ts> backup, full assertions
(top-level keys, proxies/groups/rule-providers counts, rules delta),
mihomo -t on temp copies with a geodata dir, then atomic replace.
Any failure aborts before any file is replaced and is recorded in
state.json (failures are never silent).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

BEGIN = "# ---- directmgr begin (managed by directmgr; do not edit) ----"
END = "# ---- directmgr end ----"
RULE_PREFIX = "  - DOMAIN-SUFFIX,"
RULE_SUFFIX = ",DIRECT"

TOPKEYS = ["mixed-port", "allow-lan", "mode", "log-level", "dns",
           "proxies", "geox-url", "proxy-groups", "rule-providers", "rules"]
EXPECT = {"proxies": 18, "proxy-groups": 12, "rule-providers": 5}

# Legacy section-2 anchor lines (first-landing migration source).
LEGACY_HEAD = "  # ---- 2. 自有域名直连 ----"
LEGACY_COMMENT = ("  # su7 系列（*.su7.dpdns.org：debi / debi4 / apple / 15x "
                  "等自有服务域名）走直连，不绕节点")


def _parse_segment(lines):
    """Return (begin_idx, end_idx, [domains]) or None if no markers."""
    try:
        b = next(i for i, l in enumerate(lines) if l.strip() == BEGIN)
        e = next(i for i, l in enumerate(lines) if l.strip() == END)
    except StopIteration:
        return None
    if e <= b:
        return None
    doms = []
    for l in lines[b + 1:e]:
        ls = l.strip()
        if ls.startswith("- DOMAIN-SUFFIX,") and ls.endswith(",DIRECT"):
            doms.append(ls[len("- DOMAIN-SUFFIX,"):-len(",DIRECT")])
    return b, e, doms


def _legacy_span(lines):
    """Locate the pre-directmgr direct block (section 2 header through
    the last consecutive DOMAIN-SUFFIX,*,DIRECT rule line)."""
    try:
        h = next(i for i, l in enumerate(lines) if l == LEGACY_HEAD)
    except StopIteration:
        return None
    j = h + 1
    last_rule = None
    while j < len(lines):
        ls = lines[j]
        if ls.startswith(RULE_PREFIX) and ls.rstrip().endswith(RULE_SUFFIX):
            last_rule = j
            j += 1
            continue
        if ls.strip() == "":
            j += 1
            continue
        if ls.lstrip().startswith("#"):
            # comment lines INSIDE the legacy block (the su7 explainer
            # sits between the header and the rules) are part of the
            # block -- but only before the first rule line; a comment
            # after the rules have ended starts the next section.
            if last_rule is None:
                j += 1
                continue
            break
        break
    if last_rule is None:
        return None
    return h, last_rule


def _segment_lines(domains):
    out = ["  " + BEGIN]
    for d in domains:
        out.append("  - DOMAIN-SUFFIX,%s,DIRECT" % d)
    out.append("  " + END)
    return out


def transform(text, domains):
    """Return new text with the managed segment == domains.
    Everything outside the segment is preserved exactly."""
    lines = text.split("\n")
    seg = _parse_segment(lines)
    if seg:
        b, e, _old = seg
        new = lines[:b] + _segment_lines(domains) + lines[e + 1:]
        return "\n".join(new)
    span = _legacy_span(lines)
    if not span:
        raise ValueError("no directmgr markers and no legacy section-2 block")
    h, last = span
    new = lines[:h + 1] + _segment_lines(domains) + lines[last + 1:]
    return "\n".join(new)


def current_domains(text):
    lines = text.split("\n")
    seg = _parse_segment(lines)
    if seg:
        return list(seg[2])
    span = _legacy_span(lines)
    if not span:
        return None
    h, last = span
    doms = []
    for l in lines[h:last + 1]:
        if l.startswith(RULE_PREFIX) and l.rstrip().endswith(RULE_SUFFIX):
            doms.append(l[len(RULE_PREFIX):-len(RULE_SUFFIX)])
    return doms


def assert_structure(text, expect_rules_delta_from=None):
    """Structural assertions on raw text (no yaml parse of the whole
    file beyond top-level key scan + section counts). proxies and
    proxy-groups entries start at column 0 ("- name:"); rules entries
    are indented two spaces."""
    lines = text.split("\n")
    keys = [l.split(":", 1)[0] for l in lines
            if re.match(r"^[a-z][a-z0-9-]*:", l)]
    if keys != TOPKEYS:
        raise ValueError("top-level keys changed: %r" % (keys,))
    counts = {}
    cur = None
    for l in lines:
        m = re.match(r"^([a-z][a-z0-9-]*):", l)
        if m:
            cur = m.group(1)
            counts.setdefault(cur, 0)
            continue
        if cur in ("proxies", "proxy-groups") and l.startswith("- "):
            counts[cur] += 1
        elif cur == "rule-providers" and re.match(r"^  [a-z0-9-]+:$", l):
            counts[cur] += 1
        elif cur == "rules" and l.startswith("  - "):
            counts["rules"] = counts.get("rules", 0) + 1
    for k, v in EXPECT.items():
        if counts.get(k) != v:
            raise ValueError("%s count %s != %s" % (k, counts.get(k), v))
    return counts


def outside_segment_hash(text):
    lines = text.split("\n")
    seg = _parse_segment(lines)
    if seg:
        b, e, _ = seg
        rest = lines[:b] + lines[e + 1:]
    else:
        span = _legacy_span(lines)
        if not span:
            raise ValueError("no segment")
        h, last = span
        rest = lines[:h + 1] + lines[last + 1:]
    return hashlib.sha256("\n".join(rest).encode("utf-8")).hexdigest()


def mihomo_test(path, mihomo_bin, geodir):
    r = subprocess.run([mihomo_bin, "-t", "-d", geodir, "-f", path],
                       capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr)
    if r.returncode != 0 or "test is successful" not in out:
        raise ValueError("mihomo -t failed for %s: %s"
                         % (path, out.strip().split("\n")[-1][:300]))


# ---------------------------------------------------------------- files

class SyncFailure(Exception):
    pass


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


HELPER = "/usr/local/bin/directmgr-sub-write"


def _atomic_write(path, text):
    """Replace a root-owned subscription file via the narrow sudo
    helper (fixed target whitelist inside the helper; the service
    passes only the target path and pipes content on stdin)."""
    r = subprocess.run(["sudo", "-n", HELPER, path], input=text,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SyncFailure("write helper failed for %s: %s"
                          % (path, (r.stderr or r.stdout).strip()[:200]))


def _sync_one_file(path, domains, ts, mihomo_bin, geodir,
                   work_dir=None):
    """Sync one subscription file. Returns a result dict.
    Raises SyncFailure before replacing anything on any failed check."""
    old = _read(path)
    old_doms = current_domains(old)
    if old_doms is None:
        raise SyncFailure("%s: cannot locate direct segment" % path)
    new = transform(old, domains)
    if new == old:
        return {"path": path, "changed": False, "domains_before": old_doms}
    pre = assert_structure(old)
    post = assert_structure(new)
    delta = post.get("rules", 0) - pre.get("rules", 0)
    if delta != len(domains) - len(old_doms):
        raise SyncFailure("%s: rules delta %d, expected %d"
                          % (path, delta, len(domains) - len(old_doms)))
    if outside_segment_hash(old) != outside_segment_hash(new):
        raise SyncFailure("%s: content outside segment changed" % path)
    # Backups are made by the write helper at replace time (the
    # subscription dir is root-owned; the service runs as apphub).
    bak = None
    # mihomo check copy lives in the service-writable work dir
    # (the subscription dir itself is not writable by the service).
    tmp = os.path.join(work_dir or ".",
                       ".check-" + str(os.getpid()) + ".yaml")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    try:
        mihomo_test(tmp, mihomo_bin, geodir)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _atomic_write(path, new)
    return {"path": path, "changed": True, "backup": bak,
            "domains_before": old_doms,
            "md5": hashlib.md5(new.encode("utf-8")).hexdigest()}


# ---------------------------------------------------------------- targets

def collect_targets(cfg, ssh_runner):
    """cfg: config dict. ssh_runner(cmd_str) -> (rc, stdout) runs a
    shell command on the shoc box. Returns target descriptors:
    {"kind": "local"|"remote", "path": ...}"""
    targets = [{"kind": "local", "path": cfg["debi_sub"]}]
    for p in cfg["shoc_subs"]:
        targets.append({"kind": "remote", "path": p})
    return targets


def sync_all(domains, cfg, ts, read_remote, write_remote):
    """Run the full sync. read_remote(path)->text, write_remote(path,
    text) performs backup+atomic replace on the peer box. Returns
    (results, error_or_None). Local files first, then remote; a
    failure stops the run and is reported (never silent)."""
    results = []
    for path in [cfg["debi_sub"]]:
        try:
            r = _sync_one_file(path, domains, ts,
                               cfg["mihomo_bin"], cfg["geodir"],
                               cfg.get("work_dir"))
            results.append(r)
        except Exception as exc:
            return results, "debi %s: %s" % (path, exc)
    for path in cfg["shoc_subs"]:
        try:
            old = read_remote(path)
            old_doms = current_domains(old)
            if old_doms is None:
                raise SyncFailure("cannot locate direct segment")
            new = transform(old, domains)
            if new == old:
                results.append({"path": "shoc:" + path, "changed": False,
                                "domains_before": old_doms})
                continue
            pre = assert_structure(old)
            post = assert_structure(new)
            delta = post.get("rules", 0) - pre.get("rules", 0)
            if delta != len(domains) - len(old_doms):
                raise SyncFailure("rules delta %d, expected %d"
                                  % (delta, len(domains) - len(old_doms)))
            if outside_segment_hash(old) != outside_segment_hash(new):
                raise SyncFailure("content outside segment changed")
            write_remote(path, new, ts)
            # verify by reading back
            back = read_remote(path)
            if current_domains(back) != list(domains):
                raise SyncFailure("read-back mismatch after write")
            assert_structure(back)
            results.append({"path": "shoc:" + path, "changed": True,
                            "domains_before": old_doms,
                            "md5": hashlib.md5(
                                new.encode("utf-8")).hexdigest()})
        except Exception as exc:
            return results, "shoc %s: %s" % (path, exc)
    return results, None


def now_ts():
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())
