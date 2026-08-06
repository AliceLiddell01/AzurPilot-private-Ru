#!/usr/bin/env python3
import ast, csv, json, re, subprocess, sys
from pathlib import Path

BASE="e392880602bc83986f42107fc87b1ce7c1d52ef0"
PART2="1c80d2cfeb6be075102924a27557f625c3cf290f"
INV=Path(sys.argv[1]); OUT=Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)

def run(*a, check=True):
    return subprocess.run(a, check=check, text=True, encoding="utf-8",
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

def tree(ref="HEAD"):
    out={}
    pat=re.compile(r"^(\d+)\s+(\S+)\s+([0-9a-f]{40})\s+(\d+|-)\t(.+)$")
    for line in run("git","ls-tree","-r","-l",ref).splitlines():
        m=pat.match(line)
        if not m: raise RuntimeError(line)
        out[m[5]]={"sha":m[3],"size":None if m[4]=="-" else int(m[4])}
    return out

base={}
with (INV/"tracked-tree.tsv").open(encoding="utf-8",newline="") as f:
    for r in csv.DictReader(f,delimiter="\t"):
        base[r["path"]]={"sha":r["object_sha"],"size":int(r["blob_size"]) if r["blob_size"].isdigit() else None}
manifest=json.loads((INV/"manifest.json").read_text())
missing=json.loads((INV/"missing-en.json").read_text())
gen=json.loads((INV/"generated-assets-analysis.json").read_text())
assert manifest["base_sha"]==BASE and manifest["text_only_artifact"] and not manifest["binary_assets_included"]
final=tree()

refs=set(); foreign_dicts=0
for rel in gen["generated_outputs"]:
    p=Path(rel); assert p.is_file(), rel
    t=ast.parse(p.read_text(encoding="utf-8"),str(p))
    for n in ast.walk(t):
        if isinstance(n,ast.Dict):
            keys={k.value for k in n.keys if isinstance(k,ast.Constant) and isinstance(k.value,str)}
            foreign_dicts += bool(keys & {"cn","en","jp","tw"})
        if isinstance(n,ast.Call):
            for kw in n.keywords:
                if kw.arg=="file" and isinstance(kw.value,ast.Constant) and isinstance(kw.value.value,str):
                    refs.add(kw.value.value.removeprefix("./").replace("\\","/"))

orig={p:r for p,r in base.items() if p.startswith("assets/en/")}
orig_missing=[p for p in orig if p not in final]
orig_changed=[p for p,r in orig.items() if p in final and final[p]!=r]
classes={}
for c in missing["candidates"]: classes.setdefault(c["classification"],[]).append(c)
fallback=classes["GENERATED_FALLBACK"]; shared=classes["SHARED_CANONICAL_CANDIDATE"]
true=classes["TRUE_NON_GLOBAL"]; generated_only=classes["UNKNOWN_DYNAMIC_REFERENCE"]

def mismatches(items):
    miss=[]; bad=[]
    for c in items:
        rel=c["relative_path"]; src=base.get("assets/cn/"+rel); dst=final.get("assets/en/"+rel)
        if dst is None: miss.append(rel)
        elif src is None or dst!=src: bad.append(rel)
    return miss,bad

fb_miss,fb_bad=mismatches(fallback); sh_miss,sh_bad=mismatches(shared)
fb_unmapped=[c["relative_path"] for c in fallback if "assets/en/"+c["relative_path"] not in refs]
def owner(rel):
    p=Path(rel); stem=p.stem
    for s in (".BUTTON",".AREA",".COLOR"):
        if stem.endswith(s): stem=stem[:-len(s)]
    return "assets/en/"+str(p.with_name(stem+p.suffix)).replace("\\","/")
sh_unowned=[c["relative_path"] for c in shared if owner(c["relative_path"]) not in refs]
foreign=[p for p in final if p.startswith(("assets/cn/","assets/jp/","assets/tw/"))]
case={}
for p in final:
    if p.startswith("assets/en/"): case.setdefault(p.casefold(),[]).append(p)
collisions=[v for v in case.values() if len(v)>1]
gen_missing=[p for p in refs if p not in final]
true_left=[c["relative_path"] for c in true if any(f"assets/{s}/{c['relative_path']}" in final for s in ("en","cn","jp","tw"))]
go_left=[c["relative_path"] for c in generated_only if any(f"assets/{s}/{c['relative_path']}" in final for s in ("en","cn","jp","tw")) or "assets/en/"+c["relative_path"] in refs]
ocr={p:r for p,r in base.items() if p.startswith("bin/ocr_models/")}
ocr_missing=[p for p in ocr if p not in final]
ocr_changed=[p for p,r in ocr.items() if p in final and final[p]!=r]
malformed=[c["relative_path"] for c in missing["candidates"] if Path(c["relative_path"]).is_absolute() or ".." in Path(c["relative_path"]).parts]
removed={"config/deploy.template-AidLux-cn.yaml","config/deploy.template-cn.yaml","config/deploy.template-docker-cn.yaml","config/deploy.template-linux-cn.yaml","deploy/docker/Dockerfile.cn"}
assert not (removed & final.keys())
assert not any("module/config/i18n/"+x in final for x in ("ja-JP.json","zh-CN.json","zh-MIAO.json","zh-TW.json"))
assert final["uv.lock"]["sha"]=="2d2d8b26e3cc52a61b338100ea7887a348e62377"

fail=dict(original_missing=orig_missing,original_changed=orig_changed,
 fallback_missing=fb_miss,fallback_bad=fb_bad,fallback_unmapped=fb_unmapped,
 shared_missing=sh_miss,shared_bad=sh_bad,shared_unowned=sh_unowned,
 foreign=foreign,collisions=collisions,generated_missing=gen_missing,
 true_remaining=true_left,generated_only_remaining=go_left,
 ocr_missing=ocr_missing,ocr_changed=ocr_changed,malformed=malformed,
 foreign_dictionaries=foreign_dicts)
fail={k:v for k,v in fail.items() if v not in ([],0)}
if fail: raise AssertionError(json.dumps(fail,ensure_ascii=False,indent=2))

commits=run("git","rev-list","--reverse",f"{BASE}..{PART2}").splitlines()
assert len(commits)==12
history=[]; workflow=[]
for c in commits:
    subject=run("git","show","-s","--format=%s",c).strip()
    files=run("git","diff-tree","--no-commit-id","--name-only","-r",c).splitlines()
    history.append({"sha":c,"subject":subject,"files":files})
    for p in files:
        if p.startswith(".github/workflows/global-en-"):
            text=run("git","show",f"{c}:{p}",check=False)
            if text:
                item={"sha":c,"path":p,"contents_write":bool(re.search(r"contents:\s*write",text,re.I)),
                      "unsafe":any(x in text.lower() for x in ("pull_request_target","secrets.","--force","force-with-lease")) or bool(re.search(r"git\s+push[^\n]*personal/stable",text,re.I))}
                workflow.append(item); assert not item["unsafe"], item

report={"base":BASE,"part2_head":PART2,"review_head":run("git","rev-parse","HEAD").strip(),
 "commits_reviewed":len(commits),"history":history,"temporary_workflows":workflow,
 "original_en":{"checked":len(orig),"missing":0,"unexplained_changed":0,"collisions":0},
 "fallback":{"reconciled":len(fallback),"missing":0,"mismatch":0,"unmapped":0},
 "shared":{"resolved":len(shared),"unexplained":0},
 "true_non_global_removed":len(true),"generated_only_removed":len(generated_only),
 "foreign_roots":0,"generated_outputs":len(gen["generated_outputs"]),"generated_missing":0,
 "ocr":{"retained":len(ocr),"missing":0,"changed":0},"verdict":"PASS_AFTER_REMEDIATION"}
(OUT/"review.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
(OUT/"review.md").write_text(
 f"# Part 3 independent review\n\n- original EN: {len(orig)}/1558; missing 0; changed 0\n"
 f"- fallback: {len(fallback)}/1294; mismatch 0\n- shared: {len(shared)}/71; unresolved 0\n"
 f"- true non-Global: {len(true)} removed\n- generated-only: {len(generated_only)} removed\n"
 f"- foreign roots/case collisions/generated missing: 0\n- OCR: {len(ocr)}/18 unchanged\n"
 f"- original commits reviewed: {len(commits)}\n- verdict: PASS_AFTER_REMEDIATION\n",encoding="utf-8")
