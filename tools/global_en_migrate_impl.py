#!/usr/bin/env python3
from __future__ import annotations
import ast, hashlib, json, re, shutil, subprocess
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
EXPECTED_FALLBACKS=1294
EXPECTED_OUTPUTS=61
SHARED=('awaken/AWAKEN_FINISH.BUTTON.png', 'coalition/SCUTTLE_CONFIRM.BUTTON.png', 'event_hospital/GET_CLUE.BUTTON.png', 'guild/GUILD_MISSION_SELECT.BUTTON.png', 'island/GET_ITEMS_ISLAND.BUTTON.png', 'island/ISLAND_FARM_POST1.BUTTON.png', 'island/ISLAND_TRANSPORT.BUTTON.png', 'island/ISLAND_WORKING.BUTTON.png', 'island/ONES_AIR_DROP.BUTTON.png', 'island/POST_ADD.BUTTON.png', 'island/POST_GET.BUTTON.png', 'island/POST_MANAGE_BUSINESS.BUTTON.png', 'island/POST_MANAGE_PRODUCTION.BUTTON.png', 'island/POST_MANAGE_STORAGE.BUTTON.png', 'island/POST_MANAGE_TRANSPORT.BUTTON.png', 'island/POST_MANAGE_TRANSPORT1.BUTTON.png', 'island/POST_MANAGE_TRANSPORT2.BUTTON.png', 'island/POST_MANAGE_TRANSPORT3.BUTTON.png', 'island/POST_MANAGE_TRANSPORT4.BUTTON.png', 'island/POST_MANAGE_TRANSPORT5.BUTTON.png', 'island/POST_MANAGE_TRANSPORT6.BUTTON.png', 'island/POST_MANAGE_TRANSPORT7.BUTTON.png', 'island/POST_MANAGE_TRANSPORT8.BUTTON.png', 'island/POST_MANAGE_TRANSPORT9.BUTTON.png', 'island/POST_MANAGE_TRANSPORT10.BUTTON.png', 'island/POST_MANAGE_TRANSPORT11.BUTTON.png', 'island/POST_MANAGE_TRANSPORT12.BUTTON.png', 'island/POST_MANAGE_TRANSPORT13.BUTTON.png', 'island/POST_MANAGE_TRANSPORT14.BUTTON.png', 'island/POST_MANAGE_TRANSPORT15.BUTTON.png', 'island/POST_MANAGE_TRANSPORT16.BUTTON.png', 'island/POST_MANAGE_TRANSPORT17.BUTTON.png', 'island/POST_MANAGE_TRANSPORT18.BUTTON.png', 'island/POST_MANAGE_TRANSPORT19.BUTTON.png', 'island/POST_MANAGE_TRANSPORT20.BUTTON.png', 'island/POST_MANAGE_TRANSPORT21.BUTTON.png', 'island/POST_MANAGE_TRANSPORT22.BUTTON.png', 'island/POST_MANAGE_TRANSPORT23.BUTTON.png', 'island/POST_MANAGE_TRANSPORT24.BUTTON.png', 'island/POST_MANAGE_TRANSPORT25.BUTTON.png', 'island/POST_MANAGE_TRANSPORT26.BUTTON.png', 'island/POST_MANAGE_TRANSPORT27.BUTTON.png', 'island/POST_MANAGE_TRANSPORT28.BUTTON.png', 'island/POST_MANAGE_TRANSPORT29.BUTTON.png', 'island/POST_MANAGE_TRANSPORT30.BUTTON.png', 'island/POST_MANAGE_TRANSPORT31.BUTTON.png', 'island/POST_MANAGE_TRANSPORT32.BUTTON.png', 'island/POST_MANAGE_TRANSPORT33.BUTTON.png', 'island/POST_MANAGE_TRANSPORT34.BUTTON.png', 'island/POST_MANAGE_TRANSPORT35.BUTTON.png', 'island/POST_MANAGE_TRANSPORT36.BUTTON.png', 'island/POST_MANAGE_TRANSPORT37.BUTTON.png', 'island/POST_MANAGE_TRANSPORT38.BUTTON.png', 'island/POST_MANAGE_TRANSPORT39.BUTTON.png', 'island/POST_MANAGE_TRANSPORT40.BUTTON.png', 'island/POST_MANAGE_TRANSPORT41.BUTTON.png', 'island/POST_MANAGE_TRANSPORT42.BUTTON.png', 'island/POST_MANAGE_TRANSPORT43.BUTTON.png', 'island/POST_MANAGE_TRANSPORT44.BUTTON.png', 'island/POST_MANAGE_TRANSPORT45.BUTTON.png', 'island/POST_MANAGE_TRANSPORT46.BUTTON.png', 'island/POST_MANAGE_TRANSPORT47.BUTTON.png', 'island/POST_MANAGE_TRANSPORT48.BUTTON.png', 'island/POST_MANAGE_TRANSPORT49.BUTTON.png', 'island/POST_MANAGE_TRANSPORT50.BUTTON.png', 'island/POST_MANAGE_TRANSPORT51.BUTTON.png', 'island/POST_MANAGE_TRANSPORT52.BUTTON.png', 'island/POST_MANAGE_TRANSPORT53.BUTTON.png', 'island/POST_MANAGE_TRANSPORT54.BUTTON.png', 'island/POST_MANAGE_TRANSPORT55.BUTTON.png', 'island/POST_MANAGE_TRANSPORT56.BUTTON.png')
FOREIGN_ROOTS=("assets/cn","assets/jp","assets/tw")
FOREIGN_LOCALES=("ja-JP.json","zh-CN.json","zh-MIAO.json","zh-TW.json")
CN_DEPLOY=(
"config/deploy.template-AidLux-cn.yaml","config/deploy.template-cn.yaml",
"config/deploy.template-docker-cn.yaml","config/deploy.template-linux-cn.yaml",
"deploy/docker/Dockerfile.cn")
REPLACEMENTS=[('module/config/utils.py', "SERVER_TO_TIMEZONE = {\n    'cn': timedelta(hours=8),\n    'en': timedelta(hours=-7),\n    'jp': timedelta(hours=9),\n    'tw': timedelta(hours=8),\n}", "SERVER_TO_TIMEZONE = {\n    'en': timedelta(hours=-7),\n}"), ('module/config/utils.py', "def server_timezone() -> timedelta:\n    return SERVER_TO_TIMEZONE.get(server_.server, SERVER_TO_TIMEZONE['cn'])\n", 'def server_timezone() -> timedelta:\n    try:\n        return SERVER_TO_TIMEZONE[server_.server]\n    except KeyError as exc:\n        raise ValueError(f"Unsupported server timezone: {server_.server}") from exc\n'), ('module/base/resource.py', "        if 'Opsi' in next_task or 'commission' in next_task:\n            # OCR 模型即将被使用，不释放\n            models = []\n        elif next_task:\n            # 释放除 'azur_lane' 以外的 OCR 模型\n            models = ['cnocr', 'jp', 'tw']\n        else:\n            models = ['azur_lane', 'cnocr', 'jp', 'tw']\n        for model in models:\n            del_cached_property(OCR_MODEL, model)\n\n        if models:\n            cache_model_names = {\n                'azur_lane': 'azur_lane',\n                'cnocr': 'cn',\n                'jp': 'jp',\n                'tw': 'tw',\n            }\n            cache_names = [cache_model_names[model] for model in models]\n            # 默认 OCR 实例会在连续任务间保留，可能仍持有检测模型；只有空闲时\n            # 所有语言模型均已释放，才能安全清理独立的 ``det`` 缓存。\n            if not next_task:\n                cache_names.append('det')\n            released_ocr_models = release_ocr_models(\n                names=cache_names\n            )\n", "        # The Global OCR namespace is retained between active tasks.\n        models = [] if next_task else ['azur_lane']\n        for model in models:\n            del_cached_property(OCR_MODEL, model)\n\n        if models:\n            cache_names = list(models)\n            # The shared detection cache is released only while idle.\n            if not next_task:\n                cache_names.append('det')\n            released_ocr_models = release_ocr_models(names=cache_names)\n"), ('module/config/config_updater.py', "ARCHIVES_PREFIX = {\n    'cn': '档案 ',\n    'en': 'archives ',\n    'jp': '檔案 ',\n    'tw': '檔案 '\n}", "ARCHIVES_PREFIX = {\n    'en': 'archives ',\n}"), ('module/config/config_updater.py', 'from module.config.server import VALID_CHANNEL_PACKAGE, VALID_PACKAGE, VALID_SERVER_LIST, to_package, to_server', 'from module.config.server import (\n    GLOBAL_PACKAGE,\n    VALID_CHANNEL_PACKAGE,\n    VALID_PACKAGE,\n    VALID_SERVER_LIST,\n    to_package,\n    to_server,\n)'), ('module/config/config_updater.py', "server = to_server(deep_get(new, 'Alas.Emulator.PackageName', 'cn'))", "server = to_server(\n            deep_get(new, 'Alas.Emulator.PackageName', GLOBAL_PACKAGE)\n        )"), ('module/config/argument/argument.yaml', '  PackageName:\n    value: auto\n    option: [auto]', '  PackageName:\n    value: com.YoStarEN.AzurLane\n    option: [com.YoStarEN.AzurLane]'), ('module/webui/oobe.py', '        self.server = "cn"\n        self.emulator_serial = "127.0.0.1:5555"\n        self.package_name = "com.bilibili.azurlane"\n        self.server_name = "cn_android-0"', '        self.server = "en"\n        self.emulator_serial = "127.0.0.1:5555"\n        self.package_name = "com.YoStarEN.AzurLane"\n        self.server_name = "en-0"'), ('module/ocr/al_ocr.py', '        self.name = kwargs.get("name", "en")', '        self.name = kwargs.get("name", "azur_lane")'), ('module/ocr/al_ocr.py', "        name (str): 模型名称，如 'azur_lane'、'cn'、'jp'、'tw'。", "        name (str): единственный публичный Global namespace 'azur_lane'."), ('module/device/method/adb.py', '# com.bilibili.azurlane/com.manjuu.azurlane.MainActivity', '# com.YoStarEN.AzurLane/com.manjuu.azurlane.PrePermissionActivity'), ('module/device/method/wsa.py', 'cmp=com.bilibili.azurlane/xxx', 'cmp=com.YoStarEN.AzurLane/xxx'), ('module/device/method/wsa.py', '{com.bilibili.azurlane/com.manjuu.azurlane.MainAct}', '{com.YoStarEN.AzurLane/com.manjuu.azurlane.PrePermissionActivity}')]
REGEX_REPLACEMENTS=[('module/webui/oobe.py', '    def _package_label\\(self, package\\):.*?    # ─── 步骤 3：模拟器配置 ───', '    def _package_label(self, package):\n        if package != "com.YoStarEN.AzurLane":\n            raise ValueError(f"Unsupported Global package: {package}")\n        return lang.t("Gui.OOBE.ServerEN")\n\n    def _package_options(self):\n        return [\n            {\n                "label": f\'{lang.t("Gui.OOBE.ServerEN")} (com.YoStarEN.AzurLane)\',\n                "value": "com.YoStarEN.AzurLane",\n            },\n        ]\n\n    @staticmethod\n    def _server_prefixes_for_region(region):\n        return ("en",) if region == "en" else ()\n\n    def _server_name_items_for_region(self, region):\n        items = []\n        for prefix in self._server_prefixes_for_region(region):\n            for index, name in enumerate(VALID_SERVER_LIST.get(prefix, [])):\n                value = f"{prefix}-{index}"\n                items.append((value, f"[EN] {name}", value))\n        return items\n\n    def _default_server_name_for_region(self, region):\n        items = self._server_name_items_for_region(region)\n        return items[0][0] if items else "disabled"\n\n    @staticmethod\n    def _package_for_server(server):\n        if server != "en":\n            raise ValueError(f"Unsupported Global server: {server}")\n        return "com.YoStarEN.AzurLane"\n\n    # ─── 步骤 3：模拟器配置 ───'), ('module/ocr/al_ocr.py', '模型按语言区分：.*?检测模型：', '模型注册表仅暴露 Global/English `azur_lane` namespace。\n共享检测模型和 generic English recognition models 仍由同一安全缓存管理。\n\n工作线程模型：\n- OCR 推理在专用后台线程 (AlOcrQueue) 中执行，避免阻塞主循环\n- 模型使用懒加载策略，首次使用时才初始化\n- 模型缓存按 (名称, 后端, 设备, 版本) 组合键管理\n\n检测模型：'), ('module/ocr/ncnn_ocr.py', '支持的模型：.*?注意：', '支持的模型：\n- azur_lane: Global/English game recognition\n\n注意：')]

def run(*args):
    p=subprocess.run(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT,check=False)
    print("$ "+" ".join(args)); print(p.stdout)
    if p.returncode: raise RuntimeError(f"command failed {p.returncode}: {args}")
    return p

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def replace_once(path,old,new):
    p=ROOT/path; text=p.read_text(encoding="utf-8")
    if text.count(old)!=1: raise RuntimeError(f"replacement drift: {path}")
    p.write_text(text.replace(old,new),encoding="utf-8")

def regex_once(path,pattern,new):
    p=ROOT/path; text=p.read_text(encoding="utf-8")
    text,count=re.subn(pattern,new,text,count=1,flags=re.S)
    if count!=1: raise RuntimeError(f"regex drift: {path}")
    p.write_text(text,encoding="utf-8")

def copy_exact(src,dst):
    if not src.is_file(): raise FileNotFoundError(src)
    dst.parent.mkdir(parents=True,exist_ok=True)
    h=digest(src)
    if dst.exists():
        if digest(dst)!=h: raise RuntimeError(f"collision: {dst}")
        return False
    shutil.copyfile(src,dst)
    if digest(dst)!=h: raise RuntimeError(f"digest mismatch: {dst}")
    return True

def literal(node):
    return node.value if isinstance(node,ast.Constant) and isinstance(node.value,str) else None

def fallback_map():
    out={}; generated=[]
    for path in sorted((ROOT/"module").glob("*/assets.py")):
        source=path.read_text(encoding="utf-8")
        if "automatically generated by dev_tools/button_extract.py" not in source: continue
        generated.append(path)
        for node in ast.walk(ast.parse(source,str(path))):
            if not isinstance(node,ast.Assign) or not isinstance(node.value,ast.Call): continue
            value=next((k.value for k in node.value.keywords if k.arg=="file"),None)
            if not isinstance(value,ast.Dict): continue
            mapping={literal(k):literal(v) for k,v in zip(value.keys,value.values)}
            en=mapping.get("en")
            if not en: continue
            match=re.fullmatch(r"(?:\./)?assets/(cn|jp|tw)/(.+)",en)
            if not match: continue
            rel=match.group(2); normalized=en.removeprefix("./")
            old=out.setdefault(rel,normalized)
            if old!=normalized: raise RuntimeError(f"conflicting fallback: {rel}")
    if len(generated)!=EXPECTED_OUTPUTS: raise RuntimeError(f"generated outputs={len(generated)}")
    if len(out)!=EXPECTED_FALLBACKS: raise RuntimeError(f"fallbacks={len(out)}")
    return out

def generated_hashes():
    return {str(p.relative_to(ROOT)):digest(p) for p in sorted((ROOT/"module").glob("*/assets.py"))
            if "automatically generated by dev_tools/button_extract.py" in p.read_text(encoding="utf-8")}

def config_hashes():
    paths=("module/config/argument/args.json","module/config/argument/menu.json",
           "module/config/config_generated.py","config/template.json",
           "module/config/i18n/ru-RU.json")
    return {p:digest(ROOT/p) for p in paths}

def validate_generated():
    generated=generated_hashes()
    if len(generated)!=EXPECTED_OUTPUTS: raise RuntimeError(f"outputs={len(generated)}")
    pattern=re.compile(r"(?:\./)?assets/(en|cn|jp|tw)/([^'\"\s]+)")
    for rel in generated:
        path=ROOT/rel; source=path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source,str(path))):
            if isinstance(node,ast.Dict):
                keys={k.value for k in node.keys if isinstance(k,ast.Constant) and isinstance(k.value,str)}
                if keys & {"cn","en","jp","tw"}: raise RuntimeError(f"server dict: {path}")
        for m in pattern.finditer(source):
            if m.group(1)!="en": raise RuntimeError(f"foreign path: {path} {m.group(0)}")
            if not (ROOT/"assets/en"/m.group(2)).is_file(): raise FileNotFoundError(m.group(2))

def parse_structured():
    for p in ROOT.rglob("*"):
        if not p.is_file(): continue
        if p.suffix==".json": json.loads(p.read_text(encoding="utf-8"))
        elif p.suffix in (".yaml",".yml"): list(yaml.safe_load_all(p.read_text(encoding="utf-8")))

def main():
    copied=0
    for rel,src in sorted(fallback_map().items()):
        copied+=copy_exact(ROOT/src,ROOT/"assets/en"/rel)
    shared=0
    for rel in SHARED:
        shared+=copy_exact(ROOT/"assets/cn"/rel,ROOT/"assets/en"/rel)
    if len(SHARED)!=71: raise RuntimeError("shared list drift")
    print(f"copied fallback={copied} shared={shared}")

    for path,old,new in REPLACEMENTS: replace_once(path,old,new)
    for path,pattern,new in REGEX_REPLACEMENTS: regex_once(path,pattern,new)

    run("uv","run","--locked","-m","dev_tools.button_extract")
    first=generated_hashes(); validate_generated()
    run("uv","run","--locked","-m","dev_tools.button_extract")
    if generated_hashes()!=first: raise RuntimeError("button generator not idempotent")

    run("uv","run","--locked","-m","module.config.config_updater")
    first=config_hashes()
    run("uv","run","--locked","-m","module.config.config_updater")
    if config_hashes()!=first: raise RuntimeError("config generator not idempotent")

    for root in FOREIGN_ROOTS: shutil.rmtree(ROOT/root)
    for locale in FOREIGN_LOCALES: (ROOT/"module/config/i18n"/locale).unlink()
    for path in CN_DEPLOY: (ROOT/path).unlink()
    validate_generated(); parse_structured()
    if len([p for p in (ROOT/"bin/ocr_models").rglob("*") if p.is_file()])!=18:
        raise RuntimeError("OCR inventory changed")
    for path in (".github/workflows/global-en-inventory.yml","tools/global_en_inventory.py"):
        if (ROOT/path).exists(): raise RuntimeError(f"old inventory remains: {path}")

    (ROOT/".github/workflows/global-en-migration.yml").unlink()
    Path(__file__).unlink()
    run("git","diff","--check")
    print("Global/EN migration complete")

if __name__=="__main__": main()
