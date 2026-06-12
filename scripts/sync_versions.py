#!/usr/bin/env python3
import sys
import re
import json
from pathlib import Path

def update_python_version(new_version):
    version_file = Path("wactorz/_version.py")
    if version_file.exists():
        content = version_file.read_text()
        new_content = re.sub(r'__version__ = ".*"', f'__version__ = "{new_version}"', content)
        version_file.write_text(new_content)
        print(f"Updated {version_file}")

def update_package_json(new_version):
    files = [Path("frontend/package.json"), Path("node/package.json")]
    for package_file in files:
        if package_file.exists():
            with open(package_file, 'r') as f:
                data = json.load(f, strict=False)
            data['version'] = new_version
            with open(package_file, 'w') as f:
                json.dump(data, f, indent=2)
                f.write('\n')
            print(f"Updated {package_file}")

def update_tauri_conf(new_version):
    tauri_file = Path("frontend/src-tauri/tauri.conf.json")
    if tauri_file.exists():
        with open(tauri_file, 'r') as f:
            data = json.load(f, strict=False)
        data['version'] = new_version
        with open(tauri_file, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        print(f"Updated {tauri_file}")

def update_ha_addon_config(new_version):
    ha_file = Path("ha-addon/config.yaml")
    if ha_file.exists():
        content = ha_file.read_text()
        new_content = re.sub(r'^version: ".*"', f'version: "{new_version}"', content, flags=re.MULTILINE)
        ha_file.write_text(new_content)
        print(f"Updated {ha_file}")

def update_cargo_toml(new_version):
    cargo_file = Path("Cargo.toml")
    if cargo_file.exists():
        content = cargo_file.read_text()
        # Look for [workspace.package] section and then version
        lines = content.splitlines()
        in_workspace_package = False
        new_lines = []
        updated = False
        for line in lines:
            if line.strip() == "[workspace.package]":
                in_workspace_package = True
                new_lines.append(line)
            elif in_workspace_package and line.strip().startswith("version ="):
                new_lines.append(f'version = "{new_version}"')
                in_workspace_package = False
                updated = True
            else:
                new_lines.append(line)
        
        if not updated:
             # Fallback to simple regex if line-by-line failed
             new_content = re.sub(r'version = ".*"', f'version = "{new_version}"', content, count=1)
        else:
             new_content = "\n".join(new_lines) + "\n"
             
        cargo_file.write_text(new_content)
        print(f"Updated {cargo_file}")

def update_docs_landing(new_version):
    landing_file = Path("docs/_landing.html")
    if landing_file.exists():
        content = landing_file.read_text(encoding="utf-8")
        new_content = re.sub(r'<div class="hero-badge">v.* · Alpha</div>', f'<div class="hero-badge">v{new_version} · Alpha</div>', content)
        landing_file.write_text(new_content, encoding="utf-8")
        print(f"Updated {landing_file}")

def update_docs_versions_json(new_version):
    v_file = Path("docs/versions.json")
    if v_file.exists():
        try:
            with open(v_file, 'r') as f:
                data = json.load(f)
            # Check if version already exists
            exists = any(v.get('version') == new_version for v in data)
            if not exists:
                data.append({"version": new_version, "title": new_version})
                with open(v_file, 'w') as f:
                    json.dump(data, f, indent=2)
                    f.write('\n')
                print(f"Updated {v_file}")
        except Exception as e:
            print(f"Warning: Could not update {v_file}: {e}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/sync_versions.py <new_version>")
        sys.exit(1)
    
    new_version = sys.argv[1]
    if new_version.startswith('v'):
        new_version = new_version[1:]

    update_python_version(new_version)
    update_package_json(new_version)
    update_tauri_conf(new_version)
    update_ha_addon_config(new_version)
    update_cargo_toml(new_version)
    update_docs_landing(new_version)
    update_docs_versions_json(new_version)

    print(f"\nAll versions synced to {new_version}!")

if __name__ == "__main__":
    main()
