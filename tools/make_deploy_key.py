#!/usr/bin/env python3
"""
Create an SSH deploy key WITHOUT a password for cPanel Git Version Control,
for hosts where cPanel's "Generate a New Key" form insists on a password
(cPanel's Git can't use a key that has one).

Run it on the hosting account, e.g. Setup Python App -> "Execute python
script" -> make_deploy_key.py -> Run Script. It:
  * creates ~/.ssh/apqms_deploy (+ .pub), type ed25519, no password
    (leaves any existing key alone; re-running reuses the same key)
  * adds a block to ~/.ssh/config so connections to GitHub use this key
  * prints the public key and also saves it to ~/apqms_deploy_key_PUBLIC.txt

Then add the PUBLIC key to GitHub: repo -> Settings -> Deploy keys -> Add.
Never share the private key file (~/.ssh/apqms_deploy).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

KEY_NAME = 'apqms_deploy'
MARK = '# --- APQMS deploy key (added by tools/make_deploy_key.py) ---'
CONFIG_BLOCK = f"""
{MARK}
Host github.com ssh.github.com
    User git
    IdentityFile ~/.ssh/{KEY_NAME}
    IdentitiesOnly yes
"""


def generate(key):
    tool = shutil.which('ssh-keygen') or ('/usr/bin/ssh-keygen' if os.path.exists('/usr/bin/ssh-keygen') else None)
    if tool:
        subprocess.run([tool, '-q', '-t', 'ed25519', '-N', '', '-C', 'cpanel-apqms-deploy', '-f', str(key)],
                       check=True)
        return
    try:      # fallback when the ssh-keygen program isn't available
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        sys.exit('Neither ssh-keygen nor the "cryptography" package is available. '
                 'Install it (pip install cryptography) and run again.')
    priv = Ed25519PrivateKey.generate()
    key.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                       serialization.NoEncryption()))
    pub = priv.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    Path(str(key) + '.pub').write_text(pub.decode() + ' cpanel-apqms-deploy\n')


def main():
    home = Path.home()
    ssh = home / '.ssh'
    ssh.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ssh, 0o700)
    key = ssh / KEY_NAME
    pub = ssh / (KEY_NAME + '.pub')

    if key.exists() and pub.exists():
        print(f'Key already exists, reusing it: {key}')
    else:
        for p in (key, pub):
            if p.exists():
                p.unlink()
        generate(key)
        print(f'Created key without password: {key}')
    os.chmod(key, 0o600)
    os.chmod(pub, 0o644)

    config = ssh / 'config'
    text = config.read_text() if config.exists() else ''
    if MARK not in text:
        config.write_text(text.rstrip('\n') + ('\n' if text else '') + CONFIG_BLOCK)
        print(f'Added GitHub settings to {config}')
    else:
        print(f'{config} already has the GitHub settings')
    os.chmod(config, 0o600)

    public = pub.read_text().strip()
    (home / 'apqms_deploy_key_PUBLIC.txt').write_text(public + '\n')
    print('\n=== PUBLIC key — paste this into GitHub -> Settings -> Deploy keys ===')
    print(public)
    print('=== (also saved in ~/apqms_deploy_key_PUBLIC.txt) ===')


if __name__ == '__main__':
    main()
