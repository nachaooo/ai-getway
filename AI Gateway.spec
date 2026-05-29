# -*- mode: python ; coding: utf-8 -*-
import os, re
from PyInstaller.utils.hooks import collect_data_files

_with_version = re.search(r'VERSION = "([^"]+)"', open('app.py', encoding='utf-8', errors='replace').read())
_VERSION = _with_version.group(1) if _with_version else "0.0.0"

a = Analysis(
    ['main.py'],
    pathex=[os.getcwd()],
    binaries=[],
    datas=[
        ('app.py', '.'),
        ('templates', 'templates'),
        ('static', 'static'),
        ('tokenizers/deepseek', 'tokenizers/deepseek'),
    ] + collect_data_files('tiktoken'),
    hiddenimports=['app', 'tiktoken_ext.openai_public'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'transformers', 'torch', 'torchvision', 'torchaudio',
        'numpy', 'pandas', 'scipy', 'sklearn', 'cv2',
        'matplotlib', 'tensorflow', 'onnxruntime', 'pyarrow',
        'datasets', 'opentelemetry', 'pytest', 'pypdfium2',
        'pdfminer', 'av', 'soundfile', 'pycountry', 'jsonschema',
        'astor', 'lxml', 'openpyxl', 'sqlalchemy', 'fsspec',
        'Crypto', 'tkinter', '_tkinter', 'pdfplumber',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f'AI Gateway v{_VERSION}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['static\\icon.ico'],
)
