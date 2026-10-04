# PyInstaller spec for the double-click desktop app. Build: pyinstaller packaging/DMXSceneBuilder.spec
# Run from the repo root. Env: TARGET_ARCH=universal2 (macOS, optional).
import os
import sys

root = os.path.abspath(os.getcwd())
version = {}
exec(open(os.path.join(root, "scenebuilder", "__init__.py")).read(), version)

datas = [
    (os.path.join(root, "web"), "web"),
    (os.path.join(root, "data", "demo_project.json"), "data"),
    (os.path.join(root, "packaging", "icon.png"), "packaging"),
]
mac = sys.platform == "darwin"
a = Analysis([os.path.join(root, "scenebuilder", "desktop_entry.py")], pathex=[root], datas=datas,
             hiddenimports=["scenebuilder.__main__"], excludes=["unittest", "pydoc"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="DMX Scene Builder", console=False, upx=False,
          icon=os.path.join(root, "packaging", "icon.icns" if mac else "icon.ico"),
          target_arch=os.environ.get("TARGET_ARCH") or None)
coll = COLLECT(exe, a.binaries, a.datas, name="DMX Scene Builder", upx=False)
if mac:
    app = BUNDLE(coll, name="DMX Scene Builder.app", icon=os.path.join(root, "packaging", "icon.icns"),
                 bundle_identifier="com.jeffholmespresents.dmxscenebuilder",
                 info_plist={
                     "CFBundleName": "DMX Scene Builder", "CFBundleDisplayName": "DMX Scene Builder",
                     "CFBundleShortVersionString": version["__version__"], "CFBundleVersion": version["__version__"],
                     "NSHighResolutionCapable": True,
                     "NSLocalNetworkUsageDescription": "DMX Scene Builder talks to your lighting boxes (E-Box) and to iPads on your network.",
                     "LSMinimumSystemVersion": "11.0",
                 })
