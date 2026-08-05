#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, collections, datetime as dt, fnmatch, hashlib, json, re, subprocess
from pathlib import Path, PurePosixPath
import yaml

SERVERS=("en","cn","jp","tw")
LOCALES=tuple(f"module/config/i18n/{x}.json" for x in ("ru-RU","en-US","ja-JP","zh-CN","zh-MIAO","zh-TW"))
TERMS=("assets/cn","assets/jp","assets/tw","assets/en","com.bilibili.azurlane","com.YoStarEN.AzurLane","com.YoStarJP.AzurLane","com.hkmanjuu.azurlane.gp","ja-JP","zh-CN","zh-MIAO","zh-TW","en-US","ru-RU","cnocr","azur_lane_jp","deploy.template-","Dockerfile.cn")
TOK={x:re.compile(rf"(?<![A-Za-z0-9_]){x}(?![A-Za-z0-9_])") for x in SERVERS}
ASSET_RE=re.compile(r"(?:\./)?assets/(?P<s>en|cn|jp|tw)/(?P<r>[A-Za-z0-9_./@+() -]+\.(?:png|gif|jpg|jpeg|webp|json|txt|onnx|bin|param))",re.I)
PATH_CALLS={"open","Path","PurePath","PurePosixPath","PureWindowsPath","join","glob","rglob","iglob","filepath_i18n","get_file","get_assets_from_file"}
MAX=8*1024*1024

def sh(*args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True):
    p=subprocess.run(args,check=False,stdout=stdout,stderr=stderr,text=text)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args)} failed ({p.returncode}): {clean(str(p.stderr or p.stdout or ''))}")
    return p
def clean(s,n=30000):
    s="".join(c for c in s.replace("\0","\\0") if c in "\n\r\t" or ord(c)>=32)
    return s if len(s)<=n else s[:n]+f"\n...[truncated {len(s)-n} chars]"
def wj(p,v): Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def wt(p,v): Path(p).write_text(v.rstrip()+"\n",encoding="utf-8")
def tree():
    raw=sh("git","ls-tree","-r","-l","-z","--full-tree","HEAD",text=False).stdout
    out=[]
    for rec in raw.split(b"\0"):
        if not rec: continue
        meta,path=rec.split(b"\t",1); mode,typ,sha,size=meta.split()
        out.append({"path":path.decode("utf-8","surrogateescape"),"mode":mode.decode(),"type":typ.decode(),"sha":sha.decode(),"size":None if size==b"-" else int(size)})
    return out
def data(root,e):
    if e["type"]!="blob" or e["mode"]=="120000": return None
    p=root/PurePosixPath(e["path"])
    try:
        if p.is_symlink(): return None
        rp=p.parent.resolve(); rr=root.resolve()
        if rp!=rr and rr not in rp.parents: return None
        return p.read_bytes()
    except OSError: return None
def txt(b):
    if b is None or len(b)>MAX or b"\0" in b: return None
    try:return b.decode()
    except UnicodeDecodeError:return None
def cat(path):
    out=[]
    for s in SERVERS:
        if path.startswith(f"assets/{s}/"):out.append(f"assets/{s}")
    if path in LOCALES:out.append(path)
    if path.startswith("bin/ocr_models/"):out.append("bin/ocr_models")
    if fnmatch.fnmatchcase(path,"config/*-cn.yaml") or fnmatch.fnmatchcase(path,"config/**/*-cn.yaml"):out.append("config-cn-yaml")
    if path.startswith("deploy/") and "cn" in PurePosixPath(path).name.lower():out.append("deploy-cn-named")
    return out
def kind(path):
    if path.startswith(("dev_tools/","tools/",".github/workflows/")):return "generator"
    if path.startswith("module/") and path.endswith("/assets.py"):return "generated file"
    if path.startswith("tests/"):return "test"
    if "fixture" in path.lower():return "fixture"
    if path.startswith(("docs/","README",".codex/context/")) or path.endswith(".md"):return "documentation"
    if path.startswith(("module/config/argument/","config/","deploy/")) or path.endswith((".json",".yaml",".yml",".toml")):return "config schema"
    if path.endswith(".py"):return "runtime"
    return "unknown"
def matches(s):
    z={x for x in TERMS if x in s};z|={x for x,r in TOK.items() if r.search(s)};return sorted(z)
def name(n):
    if isinstance(n,ast.Name):return n.id
    if isinstance(n,ast.Attribute):
        p=name(n.value);return f"{p}.{n.attr}" if p else n.attr
    return None
def py_ast(path,s):
    o={"path":path,"parse_error":None,"imports":[],"strings":[],"path_calls":[],"server_dicts":[],"fallbacks":[],"dynamic":[]}
    try:t=ast.parse(s,path)
    except SyntaxError as e:o["parse_error"]={"line":e.lineno,"message":e.msg};return o
    docs=set()
    for n in ast.walk(t):
        if isinstance(n,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0],ast.Expr) and isinstance(n.body[0].value,ast.Constant) and isinstance(n.body[0].value.value,str):docs.add(id(n.body[0].value))
    for n in ast.walk(t):
        if isinstance(n,ast.Import):o["imports"]+=({"line":n.lineno,"module":a.name,"as":a.asname} for a in n.names)
        elif isinstance(n,ast.ImportFrom):o["imports"].append({"line":n.lineno,"module":n.module,"names":[a.name for a in n.names],"level":n.level})
        elif isinstance(n,ast.Constant) and isinstance(n.value,str):
            m=matches(n.value)
            if m:o["strings"].append({"line":getattr(n,"lineno",None),"matches":m,"kind":"docstring" if id(n) in docs else "string","sha256":hashlib.sha256(n.value.encode()).hexdigest(),"length":len(n.value)})
        elif isinstance(n,ast.Call):
            q=name(n.func);short=q.rsplit(".",1)[-1] if q else ""
            if short in PATH_CALLS:
                ls=[a.value for a in n.args if isinstance(a,ast.Constant) and isinstance(a.value,str)];o["path_calls"].append({"line":getattr(n,"lineno",None),"call":q,"literal_args":ls,"dynamic_args":len(n.args)-len(ls)})
            if q and any(x in q.lower() for x in ("server","locale","package")):o["dynamic"].append({"line":getattr(n,"lineno",None),"call":q})
        elif isinstance(n,ast.Dict):
            ks=[k.value for k in n.keys if isinstance(k,ast.Constant) and k.value in SERVERS]
            if ks:o["server_dicts"].append({"line":getattr(n,"lineno",None),"keys":sorted(set(ks))})
        elif isinstance(n,(ast.If,ast.IfExp,ast.Try,ast.Match)):
            seg=ast.get_source_segment(s,n)
            if seg and any(r.search(seg) for r in TOK.values()):o["fallbacks"].append({"line":getattr(n,"lineno",None),"node":type(n).__name__,"sha256":hashlib.sha256(seg.encode()).hexdigest()})
    return o
def scalar(v,p=()):
    if isinstance(v,dict):
        for k,x in v.items():
            k=str(k);m=matches(k)
            if m:yield {"path":list(p+(k,)),"at":"key","matches":m,"sha256":hashlib.sha256(k.encode()).hexdigest()}
            yield from scalar(x,p+(k,))
    elif isinstance(v,list):
        for i,x in enumerate(v):yield from scalar(x,p+(str(i),))
    elif isinstance(v,str):
        m=matches(v)
        if m:yield {"path":list(p),"at":"value","matches":m,"sha256":hashlib.sha256(v.encode()).hexdigest(),"length":len(v)}
def structured(path,s):
    o={"path":path,"parse_error":None,"matches":[]}
    try:v=json.loads(s) if path.endswith(".json") else list(yaml.safe_load_all(s))
    except Exception as e:o["parse_error"]=clean(str(e),2000);return o
    o["matches"]=list(scalar(v));return o
def literal(n):return n.value if isinstance(n,ast.Constant) and isinstance(n.value,str) else None
def generated(root,entries,assets):
    src=(root/"dev_tools/button_extract.py").read_text();outputs=[];maps=collections.defaultdict(list);dicts=[];errors=[]
    for e in entries:
        p=e["path"]
        if not(p.startswith("module/") and p.endswith("/assets.py")):continue
        s=txt(data(root,e))
        if not s or "automatically generated by dev_tools/button_extract.py" not in s:continue
        outputs.append(p)
        try:t=ast.parse(s,p)
        except SyntaxError as x:errors.append({"file":p,"line":x.lineno,"message":x.msg});continue
        for n in ast.walk(t):
            if not(isinstance(n,ast.Assign) and isinstance(n.value,ast.Call)):continue
            sym=n.targets[0].id if n.targets and isinstance(n.targets[0],ast.Name) else None
            d=next((k.value for k in n.value.keywords if k.arg=="file" and isinstance(k.value,ast.Dict)),None)
            if d is None:continue
            keys=[]
            for k,v in zip(d.keys,d.values):
                req=literal(k) if k else None;ap=literal(v)
                if req not in SERVERS or not ap:continue
                m=re.fullmatch(r"(?:\./)?assets/(en|cn|jp|tw)/(.+)",ap)
                if not m:continue
                keys.append(req);maps[m.group(2)].append({"file":p,"line":getattr(n,"lineno",None),"symbol":sym,"requested":req,"resolved":m.group(1),"asset_path":ap.removeprefix("./")})
            if set(keys)==set(SERVERS):dicts.append({"file":p,"line":getattr(n,"lineno",None),"symbol":sym})
    edges=[];deps=[]
    for r,mm in sorted(maps.items()):
        present=sorted(assets.get(r,{}));en=[x for x in mm if x["requested"]=="en"];non=sorted({x["resolved"] for x in en if x["resolved"]!="en"})
        if "en" not in present and non:
            deps.append({"relative_path":r,"servers_present":present,"resolved_servers":non,"mapping_count":len(en)});edges+=({"requested_server":"en","resolved_server":x["resolved"],"relative_path":r,"generated_file":x["file"],"line":x["line"],"symbol":x["symbol"]} for x in en if x["resolved"]!="en")
    shared=[r for r,sm in sorted(assets.items()) if "cn" in sm and "en" not in sm and any(x["requested"]=="en" and x["resolved"]=="cn" for x in maps.get(r,[]))]
    block=[] if not errors else [{"type":"generated-assets-parse-errors","count":len(errors),"details":errors}]
    return {"generator_inputs":["dev_tools/button_extract.py","module/config/server.py::VALID_SERVER","module/config/config_manual.py::ASSETS_FOLDER","assets/cn/**","assets/en/**","assets/jp/**","assets/tw/**"],"generated_outputs":sorted(outputs),"canonical_roots":["assets/cn"] if "ASSETS_FOLDER + '/cn'" in src else [],"fallback_edges":edges,"missing_en_dependencies":deps,"server_dictionary_outputs":dicts,"shared_candidates":shared,"blocking_unknowns":block,"parse_errors":errors,"generator_source_sha256":hashlib.sha256(src.encode()).hexdigest(),"proved_behaviors":{"module_list_source":"assets/cn directories","server_iteration_source":"VALID_SERVER","fallback_behavior":"missing server asset reuses cn metadata/path","output_pattern":"module/<asset-module>/assets.py"}},maps
def baseline(out):
    cmds=[("uv","lock","--check"),("uv","sync","--locked","--group","ci"),("uv","run","--locked","-m","dev_tools.button_extract"),("uv","run","--locked","-m","module.config.config_updater")];rec=[];log=[];ok=True
    for c in cmds:
        p=sh(*c,check=False);a=clean(str(p.stdout or ""));b=clean(str(p.stderr or ""));rec.append({"command":c,"returncode":p.returncode,"stdout_sha256":hashlib.sha256(a.encode()).hexdigest(),"stderr_sha256":hashlib.sha256(b.encode()).hexdigest()});log.append(f"$ {' '.join(c)}\nreturncode={p.returncode}\nstdout:\n{a}\nstderr:\n{b}\n")
        if p.returncode:ok=False;break
    de=sh("git","diff","--exit-code",check=False,stdout=subprocess.DEVNULL);dc=sh("git","diff","--check",check=False);st=sh("git","status","--porcelain=v1",check=False);ns=sh("git","diff","--name-status",check=False);nu=sh("git","diff","--numstat",check=False)
    cleanbase=ok and de.returncode==0 and dc.returncode==0 and not str(st.stdout or "").strip();o={"commands":rec,"git_diff_exit_code":de.returncode,"git_diff_check_returncode":dc.returncode,"git_status_porcelain":clean(str(st.stdout or ""),10000),"git_diff_name_status":clean(str(ns.stdout or ""),10000),"git_diff_numstat":clean(str(nu.stdout or ""),10000),"clean":cleanbase};log.append(f"git diff --exit-code={de.returncode}\ngit diff --check={dc.returncode}\ngit status --porcelain:\n{o['git_status_porcelain']}\ngit diff --name-status:\n{o['git_diff_name_status']}\ngit diff --numstat:\n{o['git_diff_numstat']}\nclean={cleanbase}\n");wj(out/"generator-baseline.json",o);wt(out/"generator-baseline.txt","\n".join(log));return o
def row(path,decision,why,m=None,role="none",replacement="n/a"):
    m=m or {"count":1,"blob_bytes":None};return {"path_or_category":path,"count":m.get("count"),"blob_bytes":m.get("blob_bytes"),"references":"see reference-graph.json","generator_role":role,"en_replacement":replacement,"decision":decision,"rationale":why}
def main():
    a=argparse.ArgumentParser()
    for x in ("output-dir","repository","branch","base-sha","head-sha","run-id","run-attempt"):a.add_argument("--"+x,required=True)
    z=a.parse_args();root=Path.cwd();out=Path(z.output_dir);out.mkdir(parents=True,exist_ok=True);head=sh("git","rev-parse","HEAD").stdout.strip()
    if head!=z.head_sha:raise SystemExit(f"exact-head mismatch: {head} != {z.head_sha}")
    entries=tree();by_path={e["path"]:e for e in entries};blobbytes=sum(e["size"] or 0 for e in entries if e["type"]=="blob");syms=sorted(e["path"] for e in entries if e["mode"]=="120000");subs=sorted(e["path"] for e in entries if e["mode"]=="160000");lfs=set()
    for e in entries:
        if e["type"]=="blob" and e["mode"]!="120000" and (e["size"] or 0)<=4096:
            b=data(root,e)
            if b and b.startswith(b"version https://git-lfs.github.com/spec/v1\n"):lfs.add(e["path"])
    manifest={"repository":z.repository,"branch":z.branch,"base_sha":z.base_sha,"inventory_head_sha":head,"workflow_run_id":z.run_id,"workflow_run_attempt":z.run_attempt,"utc_timestamp":dt.datetime.now(dt.timezone.utc).isoformat(),"script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"text_only_artifact":True,"binary_assets_included":False};wj(out/"manifest.json",manifest)
    wt(out/"tracked-tree.tsv","path\tmode\tobject_type\tobject_sha\tblob_size\n"+"\n".join(f"{e['path']}\t{e['mode']}\t{e['type']}\t{e['sha']}\t{'' if e['size'] is None else e['size']}" for e in entries))
    metrics=collections.defaultdict(lambda:{"count":0,"blob_bytes":0,"paths":[]})
    for e in entries:
        if e["type"]!="blob":continue
        for c in cat(e["path"]):metrics[c]["count"]+=1;metrics[c]["blob_bytes"]+=e["size"] or 0;metrics[c]["paths"].append(e["path"])
    metrics=dict(sorted(metrics.items()));wj(out/"category-metrics.json",{"total_tracked_entries":len(entries),"total_tracked_blob_bytes":blobbytes,"symlink_count":len(syms),"symlinks":syms,"submodule_count":len(subs),"submodules":subs,"lfs_pointer_count":len(lfs),"lfs_pointers":sorted(lfs),"lfs_caveat":"pointer blob size is not external object size" if lfs else "No Git LFS pointers detected.","categories":metrics})
    assets=collections.defaultdict(dict);records=[]
    for e in entries:
        q=PurePosixPath(e["path"]).parts
        if len(q)<3 or q[0]!="assets" or q[1] not in SERVERS:continue
        b=data(root,e);r=PurePosixPath(*q[2:]).as_posix();x={"server":q[1],"relative_path":r,"path":e["path"],"blob_sha":e["sha"],"sha256":hashlib.sha256(b).hexdigest() if b is not None else None,"blob_size":e["size"],"is_symlink":e["mode"]=="120000","is_lfs_pointer":e["path"] in lfs};records.append(x);assets[r][q[1]]=x
    groups=collections.defaultdict(list);dupes=[]
    for r,sm in sorted(assets.items()):
        ss=sorted(sm);hs={s:sm[s]["blob_sha"] for s in ss};g=[]
        if len(ss)==1:g.append("ONLY_"+ss[0].upper())
        if set(ss)==set(SERVERS) and len(set(hs.values()))==1:g.append("IDENTICAL_ALL_SERVERS")
        for s in ("cn","jp","tw"):
            if "en" in hs and s in hs and hs["en"]==hs[s]:g.append("IDENTICAL_EN_"+s.upper())
        if len(ss)>1 and len(set(hs.values()))>1:g.append("SAME_RELATIVE_PATH_DIFFERENT_CONTENT")
        x={"relative_path":r,"servers":ss,"blob_shas":hs,"sha256":{s:sm[s]["sha256"] for s in ss},"blob_sizes":{s:sm[s]["blob_size"] for s in ss},"groups":sorted(set(g))};dupes.append(x)
        for k in x["groups"]:groups[k].append(x)
    dd={"group_counts":{k:len(v) for k,v in sorted(groups.items())},"groups":dict(sorted(groups.items())),"entries":dupes,"asset_records":records};wj(out/"duplicate-assets.json",dd)
    refs=[];pys=[];struct=[];parse_errors=0
    for e in entries:
        s=txt(data(root,e))
        if s is None:continue
        for ln,line in enumerate(s.splitlines(),1):
            found={x for x in TERMS if x in line}|{x for x,r in TOK.items() if r.search(line)};found|={f"assets/{m.group('s').lower()}/{m.group('r').strip()}" for m in ASSET_RE.finditer(line)};refs+=({"path":e["path"],"line":ln,"term":x,"classification":kind(e["path"])} for x in sorted(found))
        if e["path"].endswith(".py"):
            x=py_ast(e["path"],s);pys.append(x);parse_errors+=bool(x["parse_error"])
        if e["path"].endswith((".json",".yaml",".yml")):
            x=structured(e["path"],s);struct.append(x);parse_errors+=bool(x["parse_error"])
    graph={"static_references":refs,"python_ast":pys,"structured_files":struct,"summary":{"static_reference_count":len(refs),"python_files_analyzed":len(pys),"structured_files_analyzed":len(struct),"parse_errors":parse_errors,"by_classification":dict(sorted(collections.Counter(x["classification"] for x in refs).items())),"by_term":dict(sorted(collections.Counter(x["term"] for x in refs).items()))}};wj(out/"reference-graph.json",graph)
    ga,maps=generated(root,entries,assets);wj(out/"generated-assets-analysis.json",ga);refcnt=collections.Counter(x["term"] for x in refs if x["term"].startswith("assets/"));miss=[];mc=collections.Counter()
    for r,sm in sorted(assets.items()):
        if "en" in sm:continue
        mm=maps.get(r,[]);non=sorted({x["resolved"] for x in mm if x["requested"]=="en" and x["resolved"]!="en"});n=sum(refcnt[f"assets/{s}/{r}"] for s in sm);c="GENERATED_FALLBACK" if non else "SHARED_CANONICAL_CANDIDATE" if sorted(sm)==["cn"] and n==0 else "TRUE_NON_GLOBAL" if any(s in sm for s in ("jp","tw")) and n==0 else "UNKNOWN_DYNAMIC_REFERENCE" if n else "DEAD_UNREFERENCED";mc[c]+=1;miss.append({"relative_path":r,"servers_present":sorted(sm),"generated_mapping_count":len(mm),"generated_path_servers":non,"static_literal_reference_count":n,"classification":c})
    missing={"count":len(miss),"classification_counts":dict(sorted(mc.items())),"candidates":miss};wj(out/"missing-en.json",missing)
    decisions=[row("assets/en/**","KEEP_GLOBAL","Global game UI asset root.",metrics.get("assets/en"),"server-specific source root"),row("assets/cn/**","REFACTOR_BEFORE_DELETE","Canonical module inventory and fallback source.",metrics.get("assets/cn"),"canonical root/fallback",f"{missing['count']} missing-EN candidates"),row("assets/jp/**","DELETE_AFTER_GENERATION_PROOF","Non-Global root; remove after mappings are rebased.",metrics.get("assets/jp"),"server source","assets/en or shared"),row("assets/tw/**","DELETE_AFTER_GENERATION_PROOF","Non-Global root; remove after mappings are rebased.",metrics.get("assets/tw"),"server source","assets/en or shared"),row("module/config/i18n/ru-RU.json","KEEP_GLOBAL","Only supported UI locale.",metrics.get(LOCALES[0])),row("module/config/i18n/en-US.json","REFACTOR_BEFORE_DELETE","Legacy/generated locale may still feed generators.",metrics.get(LOCALES[1]),"legacy locale","ru-RU")];decisions += [row(p,"DELETE_NON_GLOBAL","Unsupported non-Global UI locale.",metrics.get(p),replacement="ru-RU") for p in LOCALES[2:]];decisions += [row("bin/ocr_models/**","KEEP_SHARED","Global/EN recognition plus shared detection/generic models.",metrics.get("bin/ocr_models"),"OCR registry"),row("config/**/*-cn.yaml","DELETE_NON_GLOBAL","CN deploy templates.",metrics.get("config-cn-yaml"),replacement="Global deploy template"),row("deploy/**/*cn*","DELETE_NON_GLOBAL","CN-named deploy variants.",metrics.get("deploy-cn-named"),replacement="Global deploy path")]
    gb=sum((by_path[p]["size"] or 0) for p in ga["generated_outputs"] if p in by_path);decisions.append(row("module/**/assets.py (generated)","REFACTOR_BEFORE_DELETE","Generated server dictionaries contain multi-server maps/fallbacks.",{"count":len(ga["generated_outputs"]),"blob_bytes":gb},"generated output","EN/shared mapping"))
    for p in ("module/config/server.py","module/config/locale.py","module/config/utils.py","module/base/resource.py","dev_tools/button_extract.py"):
        e=by_path.get(p);decisions.append(row(p,"REFACTOR_BEFORE_DELETE","Live defaults, fallbacks, cache names, or generator behavior.",{"count":1 if e else 0,"blob_bytes":e["size"] if e else 0},"generator owner" if p.startswith("dev_tools") else "runtime/config owner","Global/EN-only contract"))
    decisions.append(row("documentation/comments/technical identifiers","OUT_OF_SCOPE","Retain neutral identifiers and document-only history.",{"count":None,"blob_bytes":None}));base=baseline(out);blocking=list(ga["blocking_unknowns"])
    if parse_errors:blocking.append({"type":"parse-errors","count":parse_errors})
    if not base["clean"]:blocking.append({"type":"generator-baseline-drift-or-failure","details_file":"generator-baseline.json"})
    findings={"decisions":decisions,"blocking_unknowns":blocking,"production_cleanup_performed":False,"runtime_config_assets_changed_by_inventory_script":False,"part1_ready":not blocking,"checks":{"exact_head":True,"full_tracked_tree":bool(entries),"duplicate_analysis":True,"missing_en_analysis":True,"generator_dependency_graph":True,"static_reference_graph":True,"python_ast_analysis":True,"json_yaml_analysis":True,"generator_baseline_clean":base["clean"],"artifact_text_only":True}};wj(out/"findings.json",findings)
    lines=["# Global/EN inventory summary","",f"- Repository: `{z.repository}`",f"- Branch: `{z.branch}`",f"- Base SHA: `{z.base_sha}`",f"- Head SHA: `{head}`",f"- Run: `{z.run_id}` attempt `{z.run_attempt}`","",f"- Tracked entries: **{len(entries)}**",f"- Tracked blob bytes: **{blobbytes}**",f"- Symlinks: **{len(syms)}**",f"- Submodules: **{len(subs)}**",f"- LFS pointers: **{len(lfs)}**",""];lines += [f"- `assets/{s}/**`: {metrics.get(f'assets/{s}',{}).get('count',0)} files, {metrics.get(f'assets/{s}',{}).get('blob_bytes',0)} bytes" for s in SERVERS];lines += ["",f"- Missing EN: **{missing['count']}**",f"- Generated outputs: **{len(ga['generated_outputs'])}**",f"- Fallback edges: **{len(ga['fallback_edges'])}**",f"- Shared candidates: **{len(ga['shared_candidates'])}**",f"- Blocking unknowns: **{len(blocking)}**",f"- Generator baseline clean: **{base['clean']}**","", "| Path/category | Count | Blob bytes | Decision |","|---|---:|---:|---|"];lines += [f"| `{x['path_or_category']}` | {x['count']} | {x['blob_bytes']} | `{x['decision']}` |" for x in decisions];wt(out/"summary.md","\n".join(lines))
    need={"manifest.json","summary.md","tracked-tree.tsv","category-metrics.json","duplicate-assets.json","missing-en.json","reference-graph.json","generated-assets-analysis.json","generator-baseline.txt","generator-baseline.json","findings.json"};got={p.name for p in out.iterdir() if p.is_file()}
    if need-got:raise SystemExit(f"missing artifact files: {sorted(need-got)}")
    for p in out.iterdir():
        if p.is_symlink() or not p.is_file():raise SystemExit(f"unexpected artifact entry: {p}")
        p.read_text(encoding="utf-8")
    print("\n".join(lines));return 2 if blocking else 0
if __name__=="__main__":raise SystemExit(main())
