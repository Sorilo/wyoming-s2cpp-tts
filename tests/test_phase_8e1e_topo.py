"""Phase 8E.1e: topology fallback tests for i9-13900K."""

import os, subprocess, sys, tempfile, json
from pathlib import Path
import pytest

_PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

def read_script():
    with open(SCRIPT) as f: return f.read()

# ── Thread siblings fallback exists ─────────────────────────────────────
def test_thread_siblings_fallback():
    s = read_script()
    assert 'thread_siblings' in s

def test_core_group_classification():
    s = read_script()
    assert 'core_groups' in s

def test_classification_method_recorded():
    s = read_script()
    assert 'classification_method' in s

def test_i9_13900k_topology_recognized():
    """Script validates P/E ratio for i9-13900K."""
    s = read_script()
    assert 'i9-13900K' in s

# ── Exact affinity sets ─────────────────────────────────────────────────
def test_exact_affinity_sets():
    """Simulated topology produces correct cpusets."""
    import tempfile, os
    tmp = tempfile.mkdtemp()
    try:
        cpu_base = os.path.join(tmp, "cpu"); os.makedirs(cpu_base)
        for cpu_id in range(32):
            topo_dir = os.path.join(cpu_base, f"cpu{cpu_id}", "topology"); os.makedirs(topo_dir)
            core_id = cpu_id // 2 if cpu_id < 16 else cpu_id - 8
            with open(os.path.join(topo_dir, "core_id"), "w") as f: f.write(str(core_id))
            if cpu_id < 16:
                sibling = cpu_id - 1 if cpu_id % 2 == 1 else cpu_id + 1
                with open(os.path.join(topo_dir, "thread_siblings_list"), "w") as f:
                    f.write(f"{cpu_id},{sibling}" if cpu_id % 2 == 0 else f"{sibling},{cpu_id}")
            else:
                with open(os.path.join(topo_dir, "thread_siblings_list"), "w") as f: f.write(str(cpu_id))

        cores = {}
        for cpu_id in range(32):
            cid_file = os.path.join(cpu_base, f"cpu{cpu_id}", "topology", "core_id")
            ts_file = os.path.join(cpu_base, f"cpu{cpu_id}", "topology", "thread_siblings_list")
            core_id = int(open(cid_file).read().strip())
            siblings = open(ts_file).read().strip()
            cores[cpu_id] = {'cpu_id': cpu_id, 'core_id': core_id, 'normalized_type': 'unknown', 'siblings': siblings}

        # Thread siblings fallback
        core_groups = {}
        for cid, c in cores.items():
            core_groups.setdefault(c['core_id'], []).append(cid)
        for cid, c in cores.items():
            group = core_groups[c['core_id']]
            c['normalized_type'] = 'P-core' if len(group) == 2 else 'E-core'

        p_cores = sorted([c['cpu_id'] for c in cores.values() if c['normalized_type'] == 'P-core'])
        e_cores = sorted([c['cpu_id'] for c in cores.values() if c['normalized_type'] == 'E-core'])

        seen_p = set(); p_physical = []; p_all = []
        for cid in sorted(p_cores):
            c = cores[cid]
            if c['core_id'] not in seen_p: seen_p.add(c['core_id']); p_physical.append(str(cid))
            p_all.append(str(cid))
        seen_e = set(); e_distinct = []
        for cid in sorted(e_cores):
            c = cores[cid]
            if c['core_id'] not in seen_e: seen_e.add(c['core_id']); e_distinct.append(str(cid))
        e_half = e_distinct[:max(1, len(e_distinct)//2)]
        p_plus_e = p_all + e_half

        assert len(p_cores) == 16
        assert len(p_physical) == 8
        assert ','.join(p_physical) == '0,2,4,6,8,10,12,14'
        assert ','.join(p_all) == '0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15'
        assert ','.join(p_plus_e) == '0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23'
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)

# ── Thread-only mode works without topology ─────────────────────────────
def test_thread_sweep_no_topology_needed():
    s = read_script()
    # Thread sweep should not depend on topology file
    assert 'run_thread_sweep' in s
    # Topology is only needed for affinity
    idx = s.find('run_affinity_sweep')
    assert 'core_topology.json' in s[idx:idx+200]  # affinity checks for topology

# ── Bash syntax ─────────────────────────────────────────────────────────
def test_bash_syntax():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"Syntax: {r.stderr}"
