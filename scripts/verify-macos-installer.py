#!/usr/bin/env python3
"""Read-only DMG validation; never installs or launches the application."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def manifest(app):
    if not app.is_dir() or app.is_symlink():
        raise ValueError('Application must be a real directory')
    result = {}
    for path in sorted(app.rglob('*')):
        if path.is_symlink():
            raise ValueError('Unexpected symlink inside application bundle')
        if path.is_file():
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError('Unexpected oversized application file')
            result[str(path.relative_to(app))] = {
                'size': path.stat().st_size, 'sha256': digest(path)
            }
    if set(result) != {
        'Contents/Info.plist', 'Contents/MacOS/ai-coding',
        'Contents/Resources/icon.icns', 'Contents/_CodeSignature/CodeResources',
    }:
        raise ValueError('Bundle contains unexpected or missing files')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dmg', required=True, type=Path)
    parser.add_argument('--reference-app', required=True, type=Path)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = {'scope': 'read-only package validation, not full release acceptance',
              'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'commands': [], 'package_status': 'FAIL', 'release_allowed': False}

    def run(argv, *, require_success=True):
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        proc = subprocess.run(argv, capture_output=True, timeout=90)
        report['commands'].append({
            'argv': [str(x) for x in argv], 'started_at': started,
            'ended_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'exit_code': proc.returncode,
            'stdout': proc.stdout.decode('utf-8', errors='replace'),
            'stderr': proc.stderr.decode('utf-8', errors='replace'),
        })
        if require_success and proc.returncode:
            raise RuntimeError('Validation command failed: ' + str(argv[0]))
        return proc

    try:
        if args.dmg.is_symlink() or not args.dmg.is_file():
            raise ValueError('DMG must be a regular file')
        actual_hash = digest(args.dmg)
        report.update(dmg=str(args.dmg), dmg_sha256=actual_hash,
                      dmg_size=args.dmg.stat().st_size)
        if actual_hash != args.expected_sha256:
            raise ValueError('DMG SHA-256 differs from the approved package')
        before = manifest(args.reference_app)
        run(['/usr/bin/hdiutil', 'verify', str(args.dmg)])
        with tempfile.TemporaryDirectory(prefix='aicoding-dmg-readonly-') as tmp:
            mount = Path(tmp) / 'volume'
            mount.mkdir()
            try:
                proc = run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse',
                            '-noautoopen', '-plist', '-mountpoint', str(mount),
                            str(args.dmg)])
                plistlib.loads(proc.stdout)
                report['volume_entries'] = sorted(p.name for p in mount.iterdir())
                required = {'AI Coding.app', 'Applications'}
                optional = {'.DS_Store', '.VolumeIcon.icns'}
                entries = set(report['volume_entries'])
                if not required <= entries or entries - required - optional:
                    raise ValueError('DMG root contains unexpected files')
                report['volume_display_metadata'] = {}
                for name in sorted(entries & optional):
                    item = mount / name
                    if item.is_symlink() or not item.is_file() or item.stat().st_size > 1024 * 1024:
                        raise ValueError('Unsafe installer display metadata')
                    report['volume_display_metadata'][name] = {'size': item.stat().st_size, 'sha256': digest(item)}
                if not (mount / 'Applications').is_symlink() or os.readlink(mount / 'Applications') != '/Applications':
                    raise ValueError('Unexpected Applications shortcut')
                app = mount / 'AI Coding.app'
                bundled = manifest(app)
                if bundled != before:
                    raise ValueError('Packaged bundle differs from verified installed application')
                info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
                if info.get('CFBundleShortVersionString') != args.expected_version or info.get('CFBundleVersion') != args.expected_version:
                    raise ValueError('Package version mismatch')
                if info.get('CFBundleIdentifier') != 'com.aicoding.desktop':
                    raise ValueError('Application identity mismatch')
                run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)])
                signature = run(['/usr/bin/codesign', '-dvv', str(app)])
                gatekeeper = run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=2', str(app)], require_success=False)
                report.update(version=args.expected_version, bundle_files=bundled,
                              package_matches_installed=True, signature_integrity='PASS',
                              signature_type='ad-hoc' if b'Signature=adhoc' in signature.stderr else 'other',
                              gatekeeper_pass=gatekeeper.returncode == 0,
                              notarization='NOT_VERIFIED', package_status='PASS')
            finally:
                if os.path.ismount(mount):
                    run(['/usr/bin/hdiutil', 'detach', str(mount)])
        if manifest(args.reference_app) != before or digest(args.dmg) != actual_hash:
            raise ValueError('Reference application or DMG changed during verification')
    except Exception as error:
        report.update(package_status='FAIL', error=str(error))
    report['completed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report.get(k) for k in ['package_status', 'version', 'dmg_sha256', 'gatekeeper_pass', 'signature_type', 'error']}, ensure_ascii=False))
    return 0 if report['package_status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
