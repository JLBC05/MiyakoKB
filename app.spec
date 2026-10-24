# -*- mode: python ; coding: utf-8 -*-
"""MiyakoKB backend PyInstaller specification.

Build with ``pyinstaller app.spec --noconfirm`` after the Vue production build.
The packaged executable hosts exactly the same API + Worker roles as
``python run.py`` / ``python saber_v2.py --role ...``; the terminal launcher is
the supported entry point.

Only knowledge-base runtime dependencies are bundled here.  The removed
translation pipeline (local OCR / detection / inpainting / rendering) must not
re-enter this list.
"""

import os
import shutil
import stat
from importlib.util import find_spec
from PyInstaller.utils.hooks import collect_submodules, collect_all, copy_metadata

block_cipher = None

# 项目根目录
PROJECT_ROOT = os.path.abspath(os.path.dirname(SPEC))

# ===================== 初始化收集列表 =====================
datas = [(os.path.join(PROJECT_ROOT, "version.json"), ".")]

# 合规材料随包分发：许可与第三方组件清单必须在用户拿到的产物里可见
datas.append((os.path.join(PROJECT_ROOT, "LICENSE"), "."))
datas.append((os.path.join(PROJECT_ROOT, "THIRD_PARTY_LICENSES.md"), "."))
datas.append((os.path.join(PROJECT_ROOT, "RELEASE_NOTES.md"), "."))
binaries = []
hiddenimports = []
module_collection_mode = {}

# ===================== 项目资源文件 =====================
# 1. v2 静态资源（已构建的 Vue SPA）、内置字体、提示词与 provider 清单
datas.append((os.path.join(PROJECT_ROOT, 'src', 'backend_v2', 'static'), os.path.join('src', 'backend_v2', 'static')))
datas.append((os.path.join(PROJECT_ROOT, 'src', 'backend_v2', 'resources'), os.path.join('src', 'backend_v2', 'resources')))
datas.append((os.path.join(PROJECT_ROOT, 'src', 'shared', 'text_style_defaults_factory.json'), os.path.join('src', 'shared')))
datas.append((os.path.join(PROJECT_ROOT, 'src', 'shared', 'prompt_defaults_factory.json'), os.path.join('src', 'shared')))
datas.append((os.path.join(PROJECT_ROOT, 'src', 'shared', 'ai_provider_manifest.json'), os.path.join('src', 'shared')))
datas.append((os.path.join(PROJECT_ROOT, 'openapi', 'v2.yaml'), 'openapi'))
# 存储契约 SQL：src/storage_migrator/contracts.py 用 Path(__file__).parent/"schemas"
# 定位，打包时必须把 schemas 目录放到同样的相对位置，否则全新数据目录初始化直接失败。
datas.append((os.path.join(PROJECT_ROOT, 'src', 'storage_migrator', 'schemas'), os.path.join('src', 'storage_migrator', 'schemas')))

# 2. 用户设置、提示词和凭据只存在于 v2 data root 的数据库中，
# 不把任何运行时配置或密钥打进应用包。

# ===================== 关键: 使用 collect_all 完整收集库 =====================
# chromadb 通过动态导入加载后端实现，必须完整收集。
critical_packages = [
    'chromadb',              # 向量数据库 (manga_insight / qa)
]

for pkg in critical_packages:
    if find_spec(pkg) is None:
        raise ModuleNotFoundError(f"required packaging dependency is missing: {pkg}")
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports
    print(f"[SPEC] collect_all({pkg}): OK")

# 收集元数据
metadata_packages = [
    'chromadb',
    'onnxruntime',
]
for pkg in metadata_packages:
    if find_spec(pkg) is not None:
        datas += copy_metadata(pkg)

# ===================== 隐藏导入 =====================
hiddenimports += [
    # Backend-first role dispatcher and persistence
    'src.backend_v2', 'src.backend_v2.dispatch', 'src.backend_v2.paths',
    'src.backend_v2.runtime_identity', 'src.backend_v2.import_guard',
    'src.backend_v2.api', 'src.backend_v2.api.app', 'src.backend_v2.api.entrypoint',
    'src.backend_v2.worker', 'src.backend_v2.worker.entrypoint',
    'src.backend_v2.launcher', 'src.backend_v2.launcher.entrypoint',
    'src.backend_v2.launcher.windows_job',
    'sqlalchemy', 'waitress',

    # Flask 相关
    'flask', 'werkzeug', 'werkzeug.serving', 'jinja2', 'itsdangerous', 'click',

    # 知识库算法实现（仍由 v2 services 在 Worker 内按需导入）
    'src',
    'src.core',

    # Manga Insight 模型和传输适配器
    'src.core.manga_insight', 'src.core.manga_insight.config_models',
    'src.core.manga_insight.embedding_client', 'src.core.manga_insight.vlm_client',
    'src.core.manga_insight.clients', 'src.core.manga_insight.clients.image_gen_client',

    # web import agent（网页漫画导入）
    'src.core.web_import', 'src.core.web_import.agent', 'src.core.web_import.firecrawl_tools',

    # shared runtime helpers
    'src.shared', 'src.shared.constants', 'src.shared.path_helpers',
    'src.shared.openai_helpers',  # OpenAI 客户端辅助函数

    # 图像处理 / 运行时基础库
    'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont', 'numpy',

    # 其他
    'chromadb', 'openai', 'httpx', 'yaml', 'loguru', 'requests', 'urllib3', 'certifi',
    'psutil', 'filelock', 'packaging', 'multiprocessing', 'concurrent.futures',
    'asyncio',
]

# Collect submodules
print("[SPEC] Collecting submodules...")
for mod in ['src.backend_v2', 'flask', 'werkzeug', 'jinja2']:
    hiddenimports += collect_submodules(mod)

# ===================== 排除项 =====================
# ``tkinter`` is NOT excluded: src/backend_v2/startup_failure.py uses it to show
# a native "MiyakoKB 启动失败" dialog, because the packaged executable runs
# without a console and an uncaught startup error would otherwise be invisible.
excludes = [
    'PyQt5', 'PyQt6', 'PySide2',
    'IPython', 'jupyter', 'notebook', 'pytest', 'sphinx', 'docutils',
    # 已移除的翻译流水线本地推理栈，不再进入安装包
    'torch', 'torchvision', 'transformers', 'tensorboard', 'timm',
    'rapidocr', 'manga_ocr', 'ultralytics', 'litelama', 'fugashi', 'unidic_lite',
    'onnxruntime.quantization', 'onnxruntime.training',
]

# ===================== Analysis =====================
print("[SPEC] Starting analysis...")
a = Analysis(
    ['saber_v2.py'],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    module_collection_mode=module_collection_mode,
)

# PyInstaller can pick up an unrelated ``icuuc.dll`` from the build machine's
# PATH (for example Poppler's ICU), which shadows System32 and breaks native
# extensions at runtime.
if os.name == 'nt':
    a.binaries = [
        entry
        for entry in a.binaries
        if os.path.basename(entry[0]).lower() != 'icuuc.dll'
    ]

# ===================== 打包 =====================
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    # Frozen Python ignores encoding environment variables; child pipes use UTF-8.
    [('X utf8', None, 'OPTION')],
    exclude_binaries=True,
    name='MiyakoKB',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT_ROOT, 'src', 'backend_v2', 'static', 'favicon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MiyakoKB',
)

bundle_plugins_path = os.path.join(coll.name, 'plugins')
plugins_path = os.path.join(PROJECT_ROOT, 'plugins')

if os.path.exists(bundle_plugins_path):
    shutil.rmtree(bundle_plugins_path)
if os.path.exists(plugins_path):
    shutil.copytree(
        plugins_path,
        bundle_plugins_path,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'),
    )
    if os.name == 'nt':
        os.chmod(
            bundle_plugins_path,
            os.stat(bundle_plugins_path).st_mode | stat.S_IWRITE,
        )
    for name in os.listdir(bundle_plugins_path):
        candidate = os.path.join(bundle_plugins_path, name)
        if (
            os.path.isdir(candidate)
            and not os.path.isfile(os.path.join(candidate, 'plugin.json'))
        ):
            shutil.rmtree(candidate)
