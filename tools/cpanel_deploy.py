#!/usr/bin/env python3
"""
Deploy the latest commit to cPanel shared hosting through the cPanel API
(no SSH needed). Used by .github/workflows/deploy.yml; can also be run by
hand:

    CPANEL_HOST=magnus.example.com CPANEL_USER=myuser CPANEL_API_TOKEN=XXXX \\
        python3 tools/cpanel_deploy.py [expected_commit_sha]

Steps:
  1. find the cPanel Git repository (CPANEL_REPO_ROOT, or the one cloned
     from this GitHub repo)
  2. pull the branch from GitHub into it
  3. check the clone is now at the expected commit
  4. start a deployment (runs .cpanel.yml) and wait for its result

Every API reply is checked; on any problem it prints the reply and exits 1,
so the GitHub Action shows red with the reason.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get('CPANEL_HOST', '').strip().removeprefix('https://').split('/')[0].split(':')[0]
USER = os.environ.get('CPANEL_USER', '').strip()
TOKEN = os.environ.get('CPANEL_API_TOKEN', '').strip()
REPO_ROOT = os.environ.get('CPANEL_REPO_ROOT', '').strip()
BRANCH = os.environ.get('CPANEL_BRANCH', 'main').strip()
REPO_HINT = os.environ.get('CPANEL_REPO_HINT', 'APQMS-').strip()
WAIT_SECONDS = int(os.environ.get('CPANEL_DEPLOY_WAIT', '240'))


def fail(msg, reply=None):
    print(f'::error::{msg}')
    if reply is not None:
        print(json.dumps(reply, indent=2)[:4000])
    sys.exit(1)


def uapi(module, function, **params):
    url = f'https://{HOST}:2083/execute/{module}/{function}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'cpanel {USER}:{TOKEN}'})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:
            body = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        if e.code in (401, 403):
            fail(f'cPanel rejected the login ({e.code}). Check CPANEL_USER and CPANEL_API_TOKEN.', body[:500])
        fail(f'cPanel API {module}::{function} HTTP {e.code}', body[:1000])
    except urllib.error.URLError as e:
        fail(f'Could not reach https://{HOST}:2083 — {e.reason}. Check CPANEL_HOST.')
    try:
        reply = json.loads(body)
    except ValueError:
        fail(f'cPanel API {module}::{function} did not return JSON (wrong host or login?)', body[:500])
    return reply


def ok(reply, what):
    if reply.get('status') != 1:
        fail(f'{what} failed: {"; ".join(reply.get("errors") or []) or "unknown error"}', reply)
    for w in reply.get('warnings') or []:
        print(f'::warning::{what}: {w}')
    return reply.get('data')


def find_repo():
    repos = ok(uapi('VersionControl', 'retrieve'), 'Listing cPanel Git repositories') or []
    if REPO_ROOT:
        for r in repos:
            if r.get('repository_root', '').rstrip('/') == REPO_ROOT.rstrip('/'):
                return r
        fail(f'No cPanel Git repository at {REPO_ROOT}. Found: '
             + ', '.join(r.get('repository_root', '?') for r in repos))
    matches = [r for r in repos
               if REPO_HINT.lower() in json.dumps(r.get('source_repository') or {}).lower()]
    if len(matches) == 1:
        return matches[0]
    if not repos:
        fail('No repository in cPanel Git Version Control yet — create the clone first (DEPLOY_CPANEL.md step 3).')
    fail('Could not tell which cPanel repository to deploy; set CPANEL_REPO_ROOT. Found: '
         + ', '.join(r.get('repository_root', '?') for r in repos))


def head_of(repo):
    last = repo.get('last_update') or {}
    return (last.get('identifier') or '').strip()


def main():
    expected = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get('GITHUB_SHA', '')).strip()
    if not (HOST and USER and TOKEN):
        fail('Set CPANEL_HOST, CPANEL_USER and CPANEL_API_TOKEN.')

    repo = find_repo()
    root = repo['repository_root']
    print(f'cPanel repository: {root} (branch {repo.get("branch")}, at {head_of(repo)[:10] or "?"})')

    # Pull the branch from GitHub into the cPanel clone.
    ok(uapi('VersionControl', 'update', repository_root=root, branch=BRANCH), f'Pulling {BRANCH} from GitHub')

    repo = find_repo()
    head = head_of(repo)
    print(f'cPanel repository now at {head[:10] or "?"}')
    if not head:
        print('::warning::Could not read the cPanel clone\'s commit; skipping the commit check.')
    if expected and head and not head.startswith(expected[:7]) and not expected.startswith(head[:7]):
        fail(f'After pulling, the cPanel clone is at {head[:10]}, not {expected[:10]}. '
             'Open Git Version Control → Manage → Pull or Deploy → "Update from Remote" '
             'to see the error (often: the clone has local changes, or the deploy key is missing).', repo)

    data = ok(uapi('VersionControlDeployment', 'create', repository_root=root), 'Starting deployment') or {}
    deploy_id = data.get('deploy_id')
    print(f'Deployment started (deploy_id={deploy_id}). Waiting for it to finish…')

    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(10)
        deployments = ok(uapi('VersionControlDeployment', 'retrieve'), 'Checking deployment') or []
        mine = [d for d in deployments if deploy_id is None or str(d.get('deploy_id')) == str(deploy_id)]
        if not mine:
            continue
        d = max(mine, key=lambda x: int(x.get('deploy_id') or 0))
        ts = d.get('timestamps') or {}
        if ts.get('succeeded'):
            print(f'✅ Deployed {(d.get("repository_state") or {}).get("identifier", head)[:10]} to the live app.')
            return 0
        if ts.get('failed') or ts.get('canceled'):
            fail(f'Deployment failed. Log on the server: {d.get("log_path")}', d)
    print('::warning::Deployment was started but did not report back in time. Check Git Version Control → '
          'Manage → Pull or Deploy → "Last Deployment Information" on cPanel.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
