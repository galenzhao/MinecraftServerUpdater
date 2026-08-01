import os
import time
import shutil
import hashlib
from datetime import datetime
import logging
import requests


# CONFIGURATION
UPDATE_TO_SNAPSHOT = False
BACKUP_DIR = 'world_backups'
LOG_FILENAME = 'auto_updater.log'
RAM_INITIAL = '512m'
RAM_MAX = '3g'
SCREEN_NAME = 'minecraft'
SERVER_JAR = '../minecraft_server.jar'

MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILENAME),
        logging.StreamHandler(),
    ],
)
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def screen_session_exists(name):
    return os.system(f'screen -list | grep -q "[.]{name}[[:space:]]"') == 0


def start_server():
    logging.info('Starting server...')
    server_dir = os.path.abspath('..')
    os.system(
        f'screen -S {SCREEN_NAME} -d -m bash -c '
        f'"cd {server_dir} && java -Xms{RAM_INITIAL} -Xmx{RAM_MAX} -jar minecraft_server.jar"'
    )


def ensure_server_running():
    if screen_session_exists(SCREEN_NAME):
        logging.info(f'Screen session "{SCREEN_NAME}" is already running.')
        return
    if not os.path.exists(SERVER_JAR):
        logging.error(f'Cannot start server: {SERVER_JAR} not found.')
        return
    start_server()


# retrieve version manifest
response = requests.get(MANIFEST_URL)
response.raise_for_status()
data = response.json()

if UPDATE_TO_SNAPSHOT:
    minecraft_ver = data['latest']['snapshot']
else:
    minecraft_ver = data['latest']['release']

# get checksum of running server
if os.path.exists(SERVER_JAR):
    sha = hashlib.sha1()
    with open(SERVER_JAR, 'rb') as f:
        sha.update(f.read())
    cur_ver = sha.hexdigest()
else:
    cur_ver = ""

for version in data['versions']:
    if version['id'] != minecraft_ver:
        continue

    jsonlink = version['url']
    jar_data = requests.get(jsonlink).json()
    jar_sha = jar_data['downloads']['server']['sha1']

    logging.info(
        f'Your sha1 is {cur_ver or "(none)"}. '
        f'Latest version is {minecraft_ver} with sha1 of {jar_sha}'
    )

    if cur_ver != jar_sha:
        logging.info('Updating server...')
        link = jar_data['downloads']['server']['url']
        logging.info(f'Downloading .jar from {link}...')
        response = requests.get(link)
        response.raise_for_status()
        with open('minecraft_server.jar', 'wb') as jar_file:
            jar_file.write(response.content)
        logging.info('Downloaded.')

        if screen_session_exists(SCREEN_NAME):
            os.system(
                f"screen -S {SCREEN_NAME} -X stuff "
                f"'say ATTENTION: Server will shutdown temporarily to update in 30 seconds.^M'"
            )
            logging.info('Shutting down server in 30 seconds.')

            for i in range(20, 9, -10):
                time.sleep(10)
                os.system(
                    f"screen -S {SCREEN_NAME} -X stuff "
                    f"'say Shutdown in {i} seconds^M'"
                )

            for i in range(9, 0, -1):
                time.sleep(1)
                os.system(
                    f"screen -S {SCREEN_NAME} -X stuff "
                    f"'say Shutdown in {i} seconds^M'"
                )
            time.sleep(1)

            logging.info('Stopping server.')
            os.system(f"screen -S {SCREEN_NAME} -X stuff 'stop^M'")
            time.sleep(5)

            logging.info('Backing up world...')
            if not os.path.exists(BACKUP_DIR):
                os.makedirs(BACKUP_DIR)

            backupPath = os.path.join(
                BACKUP_DIR,
                "world_backup_"
                + datetime.now().isoformat().replace(':', '-')
                + "_sha="
                + cur_ver,
            )
            shutil.make_archive(backupPath, 'zip', "../world")
            logging.info('Backed up world.')
        else:
            logging.info('No running screen session; skipping shutdown/backup.')

        logging.info('Updating server .jar')
        if os.path.exists(SERVER_JAR):
            os.remove(SERVER_JAR)
        os.rename('minecraft_server.jar', SERVER_JAR)
        start_server()
    else:
        logging.info('Server is already up to date.')
        ensure_server_running()

    break
else:
    logging.error(f'Version {minecraft_ver} not found in Mojang manifest.')
