"""Tests for Phase 7B.3: compatibility synthesize deferral."""

import asyncio, io, json, os
import pytest
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from app.config import Settings
from app.observability import setup_logging
from app.s2_client import S2GenerateResult
from app.wyoming_server import FakeTtsConfig, start_fake_tts_server

RH = {"x-audio-encoding":"pcm_s16le","x-audio-channels":"1","x-audio-sample-rate":"44100"}
CT = "audio/L16; rate=44100; channels=1"

class _BS:
    def __init__(s,r): s.ct=r.content_type; s.rh=r.response_headers; s._a=r.audio; s._y=False
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def __iter__(s): return s
    def __next__(s):
        if s._y: raise StopIteration
        s._y=True; return s._a

class R:
    def __init__(s,a,*,ct=CT,rh=None): s.a=a; s.ct=ct; s.rh=RH.copy() if rh is None else rh; s.rq=[]
    def generate_multipart(s,r): s.rq.append(r); return S2GenerateResult(audio=s.a,content_type=s.ct,response_headers=s.rh.copy())
    def generate_stream(s,r,files=None,boundary=None): s.rq.append(r); return _BS(S2GenerateResult(audio=s.a,content_type=s.ct,response_headers=s.rh.copy()))

PCM = bytes([1,0])*200

def _pl(t):
    rs=[]
    for ln in t.strip().split("\n"):
        if not ln.strip(): continue
        try: rs.append(json.loads(ln))
        except: pass
    return rs

def _et(rs,en): return [r for r in rs if r.get("event")==en]

async def _ras(c,t=3):
    es=[]
    while True:
        e=await asyncio.wait_for(c.read_event(),timeout=t)
        if e is None: break
        es.append(e)
        if AudioStop.is_type(e.type): break
    return es

async def _rss(c,t=3):
    es=[]
    while True:
        e=await asyncio.wait_for(c.read_event(),timeout=t)
        if e is None: break
        es.append(e)
        if SynthesizeStopped.is_type(e.type): break
    return es

def _ms(rc,st=None):
    if st is None: st=Settings(tts_backend="s2cpp",s2_voice_dir="/tmp/nx")
    lp=asyncio.new_event_loop()
    sv=lp.run_until_complete(start_fake_tts_server(host="127.0.0.1",port=0,settings=st,s2_client_factory=lambda s:rc))
    return lp,sv

def test_standalone_legacy():
    r=R(PCM); c=io.StringIO()
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r)
    try:
        async def f():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(Synthesize(text="hello world").event())
                await _ras(cl)
        lp.run_until_complete(f())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    assert len(_et(rs,"syn_trigger"))==1
    assert _et(rs,"syn_trigger")[0]["trigger"]=="legacy"
    assert len(_et(rs,"backend_start"))==1
    assert len(_et(rs,"audio_out"))==1

def test_normal_streaming():
    r=R(PCM); c=io.StringIO()
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r)
    try:
        async def f():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(SynthesizeStart().event())
                await cl.write_event(SynthesizeChunk(text="hello").event())
                await cl.write_event(SynthesizeChunk(text="world").event())
                await cl.write_event(SynthesizeStop().event())
                await _rss(cl)
        lp.run_until_complete(f())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    tr=_et(rs,"syn_trigger"); assert len(tr)==1; assert tr[0]["trigger"]=="streaming"
    assert len(_et(rs,"backend_start"))==1
    assert len(_et(rs,"audio_out"))==1
    assert len(_et(rs,"syn_stopped"))==1

def test_ha_compat_sequence():
    r=R(PCM); c=io.StringIO()
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r)
    try:
        async def f():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(SynthesizeStart().event())
                await cl.write_event(SynthesizeChunk(text="hello").event())
                await cl.write_event(Synthesize(text="hello world").event())
                await cl.write_event(SynthesizeStop().event())
                await _rss(cl)
        lp.run_until_complete(f())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    df=_et(rs,"compatibility_synthesize_deferred"); assert len(df)==1; assert df[0]["status"]=="deferred"
    tr=_et(rs,"syn_trigger"); assert len(tr)==1; assert tr[0]["trigger"]=="streaming"
    assert len(_et(rs,"backend_start"))==1
    assert len(_et(rs,"backend_stream_done"))==1
    assert len(_et(rs,"audio_out"))==1
    assert len(_et(rs,"syn_stopped"))==1

def test_compat_no_chunks_fallback():
    r=R(PCM); c=io.StringIO()
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r)
    try:
        async def f():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(SynthesizeStart().event())
                await cl.write_event(Synthesize(text="fallback").event())
                await cl.write_event(SynthesizeStop().event())
                await _rss(cl)
        lp.run_until_complete(f())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    assert len(_et(rs,"compatibility_synthesize_deferred"))==1
    tr=_et(rs,"syn_trigger"); assert len(tr)==1; assert tr[0]["trigger"]=="streaming"
    assert r.rq[0].text=="fallback"

def test_compat_voice_mismatch():
    vd="/tmp/tcv2"; os.makedirs(vd,exist_ok=True)
    for p in ["voice_a","voice_b"]:
        with open(os.path.join(vd,f"{p}.s2voice"),"wb") as f: f.write(b"x")
    r=R(PCM); c=io.StringIO()
    st=Settings(tts_backend="s2cpp",s2_voice_dir=vd)
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r,st=st)
    try:
        async def f():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                se=SynthesizeStart().event()
                se.data["voice"]={"name":"voice_a"}
                await cl.write_event(se)
                await cl.write_event(Synthesize(text="test",voice=SynthesizeVoice(name="voice_b")).event())
                await asyncio.sleep(0.2)
        lp.run_until_complete(f())
    except: pass
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    vm=[d for d in _et(rs,"compatibility_synthesize_deferred") if d.get("status")=="voice_mismatch"]
    assert len(vm)>=1, f"No voice_mismatch in {rs}"

def test_two_independent_legacy():
    r=R(PCM); c=io.StringIO()
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r)
    try:
        async def r1():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(Synthesize(text="first").event()); await _ras(cl)
        lp.run_until_complete(r1())
        async def r2():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(Synthesize(text="second").event()); await _ras(cl)
        lp.run_until_complete(r2())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    tr=_et(rs,"syn_trigger"); assert len(tr)==2
    assert all(t["trigger"]=="legacy" for t in tr)
    assert len(_et(rs,"backend_start"))==2
    assert len(_et(rs,"audio_out"))==2

def test_disconnect_cleanup():
    r=R(PCM); lp,sv=_ms(r)
    try:
        async def p():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(SynthesizeStart().event())
                await cl.write_event(SynthesizeChunk(text="partial").event())
        lp.run_until_complete(p())
        async def n():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(Synthesize(text="after").event())
                return await _ras(cl)
        es=lp.run_until_complete(n())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    assert any(AudioStart.is_type(e.type) for e in es)
    assert any(AudioStop.is_type(e.type) for e in es)

def test_one_synthesis_id():
    r=R(PCM); c=io.StringIO()
    import app.observability as o; o.logger.handlers.clear()
    setup_logging("info",stream=c)
    lp,sv=_ms(r)
    try:
        async def f():
            async with AsyncTcpClient("127.0.0.1",sv.port) as cl:
                await cl.write_event(SynthesizeStart().event())
                await cl.write_event(SynthesizeChunk(text="hello").event())
                await cl.write_event(Synthesize(text="hello world").event())
                await cl.write_event(SynthesizeStop().event())
                await _rss(cl)
        lp.run_until_complete(f())
    finally: lp.run_until_complete(sv.stop()); lp.close()
    for h in o.logger.handlers: h.flush()
    rs=_pl(c.getvalue())
    sids={r.get("synthesis_id") for r in rs if r.get("synthesis_id") and r.get("event") in ("syn_trigger","backend_start","backend_done","audio_out")}
    assert len(sids)==1, f"Expected 1 sid, got {sids}"
