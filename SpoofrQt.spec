# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the native Qt Spoofr.app.

Bundles the qtui app + device core (pymobiledevice3), with the location usage
description so CoreLocation can give a precise fix from one native prompt.
Heavy unused Qt modules are excluded to keep PySide6 (1.1 GB on disk) manageable.

No `ipsw` binary any more: pymobiledevice3 11 downloads the personalized developer
image itself and never shells out to it, so the 71 MB CLI was dead weight (and it
pushed the zipped download past GitHub's 100 MB file limit).
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = [('web', 'web')]
binaries = []

hiddenimports = ['applog', 'core', 'portable', 'server', 'macui', 'pymobiledevice3.__main__',
                 'CoreLocation', 'Foundation', 'AppKit', 'objc', 'segno', 'requests']
hiddenimports += collect_submodules('qtui')
hiddenimports += collect_submodules('pymobiledevice3')
hiddenimports += collect_submodules('gpxpy')
hiddenimports += collect_submodules('geocoder')
datas += collect_data_files('pymobiledevice3')
datas += copy_metadata('pymobiledevice3', recursive=True)

# Trim the Qt that ships in PySide6 but the app never touches.
excludes = [
    'tkinter', 'customtkinter', 'tkintermapview',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQml', 'PySide6.QtQuickWidgets',
    'PySide6.QtQuickControls2', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
    'PySide6.Qt3DExtras', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
    'PySide6.QtDesigner', 'PySide6.QtUiTools', 'PySide6.QtBluetooth', 'PySide6.QtNfc',
    'PySide6.QtSerialPort', 'PySide6.QtSerialBus', 'PySide6.QtPositioning',
    'PySide6.QtLocation', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtHelp',
    'PySide6.QtWebSockets', 'PySide6.QtWebChannel', 'PySide6.QtRemoteObjects',
    'PySide6.QtScxml', 'PySide6.QtSensors', 'PySide6.QtSpatialAudio',
    'PySide6.QtTextToSpeech', 'PySide6.QtHttpServer', 'PySide6.QtDBus',
    # pymobiledevice3's interactive shell and screencast, never used here (the
    # tunnel helper's import path was checked: it touches none of these)
    'IPython', 'jedi', 'parso', 'PIL',
]

a = Analysis(
    ['spoofr_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Spoofr',
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
    icon=['Spoofr.app/Contents/Resources/AppIcon.icns'],
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='Spoofr')
app = BUNDLE(
    coll,
    name='Spoofr.app',
    icon='Spoofr.app/Contents/Resources/AppIcon.icns',
    bundle_identifier='com.spoofr.app',
    info_plist={
        'CFBundleName': 'Spoofr',
        'CFBundleDisplayName': 'Spoofr',
        'CFBundleShortVersionString': '1.0.0',
        'LSMinimumSystemVersion': '13.0',
        'NSHighResolutionCapable': True,
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSLocationWhenInUseUsageDescription':
            'Spoofr centers the map on your current location when you connect.',
        'NSLocationUsageDescription':
            'Spoofr centers the map on your current location when you connect.',
    },
)
