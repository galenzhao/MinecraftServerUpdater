import os
import time
import glob
import shutil
import hashlib
import shlex
from datetime import datetime
import logging
import requests


# CONFIGURATION
UPDATE_TO_SNAPSHOT = False
BACKUP_DIR = 'world_backups'
LOG_FILENAME = 'auto_updater.log'
SCREEN_NAME = 'minecraft'
SERVER_JAR = '../minecraft_server.jar'
# Fraction of currently available RAM to give the JVM (leave the rest free).
AVAILABLE_RAM_RATIO = 0.75
# Absolute floor reserved for the OS / other processes, in MiB.
MIN_OS_RESERVE_MB = 1024
MIN_HEAP_MB = 1024
STOP_TIMEOUT_SEC = 120

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


def _meminfo_kb():
    """Parse /proc/meminfo into a {key: kib} dict. Empty on failure."""
    info = {}
    try:
        with open('/proc/meminfo', encoding='utf-8') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(':'):
                    info[parts[0][:-1]] = int(parts[1])
    except (OSError, ValueError):
        pass
    return info


def get_available_memory_mb():
    """Prefer MemAvailable (accounts for other processes); fall back carefully."""
    info = _meminfo_kb()
    if 'MemAvailable' in info:
        return info['MemAvailable'] // 1024
    if 'MemFree' in info and 'Buffers' in info and 'Cached' in info:
        return (info['MemFree'] + info['Buffers'] + info['Cached']) // 1024
    try:
        pages = os.sysconf('SC_AVPHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
        return (pages * page_size) // (1024 * 1024)
    except (AttributeError, OSError, ValueError):
        logging.warning('Could not detect available RAM; falling back to 2G heap.')
        return 2048


def get_minecraft_rss_mb():
    """RSS of the java process running minecraft_server.jar, or 0 if not found."""
    for cmdline_path in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            with open(cmdline_path, 'rb') as f:
                cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', errors='ignore')
            if 'minecraft_server.jar' not in cmdline or 'java' not in cmdline:
                continue
            pid = cmdline_path.split('/')[2]
            with open(f'/proc/{pid}/status', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            continue
    return 0


def get_heap_budget_mb():
    """
    Memory that will be free for a (re)start.

    MemAvailable alone is wrong while MC is still running — its RSS is already
    subtracted. Add that RSS back so a restart is sized as if the old process
    had already exited (other programs still correctly excluded).
    """
    available = get_available_memory_mb()
    mc_rss = get_minecraft_rss_mb()
    if mc_rss:
        logging.info(
            f'MC already using {mc_rss}M RSS; counting it toward restart budget '
            f'(MemAvailable={available}M).'
        )
    return available + mc_rss


def choose_heap_mb(available_mb):
    # Leave headroom for the OS and other processes already using RAM.
    usable = max(0, available_mb - MIN_OS_RESERVE_MB)
    heap = int(usable * AVAILABLE_RAM_RATIO)
    heap = min(heap, usable)
    heap = (heap // 512) * 512
    if heap < MIN_HEAP_MB:
        if usable >= MIN_HEAP_MB:
            heap = MIN_HEAP_MB
        else:
            heap = max(512, (usable // 512) * 512)
            logging.warning(
                f'Low available RAM ({available_mb}M); using {heap}M heap '
                f'(below preferred minimum of {MIN_HEAP_MB}M).'
            )
    return heap


def wait_for_server_stopped(timeout=STOP_TIMEOUT_SEC):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not screen_session_exists(SCREEN_NAME) and get_minecraft_rss_mb() == 0:
            return True
        time.sleep(1)
    return False


def stop_server():
    if not screen_session_exists(SCREEN_NAME) and get_minecraft_rss_mb() == 0:
        return True
    logging.info('Stopping server.')
    if screen_session_exists(SCREEN_NAME):
        os.system(f"screen -S {SCREEN_NAME} -X stuff 'stop^M'")
    if wait_for_server_stopped():
        logging.info('Server stopped.')
        return True
    logging.error(f'Server did not stop within {STOP_TIMEOUT_SEC}s.')
    return False


def build_jvm_args(heap_mb):
    """Aikar's G1GC flags, auto-switched for heaps under/over 12G."""
    args = [
        f'-Xms{heap_mb}M',
        f'-Xmx{heap_mb}M',
        '-XX:+UseG1GC',
        '-XX:+ParallelRefProcEnabled',
        '-XX:MaxGCPauseMillis=200',
        '-XX:+UnlockExperimentalVMOptions',
        '-XX:+DisableExplicitGC',
        '-XX:+AlwaysPreTouch',
        '-XX:G1HeapWastePercent=5',
        '-XX:G1MixedGCCountTarget=4',
        '-XX:G1MixedGCLiveThresholdPercent=90',
        '-XX:G1RSetUpdatingPauseTimePercent=5',
        '-XX:SurvivorRatio=32',
        '-XX:+PerfDisableSharedMem',
        '-XX:MaxTenuringThreshold=1',
        '-Dusing.aikars.flags=https://mcflags.emc.gs',
        '-Daikars.new.flags=true',
    ]
    if heap_mb >= 12 * 1024:
        args.extend([
            '-XX:G1NewSizePercent=40',
            '-XX:G1MaxNewSizePercent=50',
            '-XX:G1HeapRegionSize=16M',
            '-XX:G1ReservePercent=15',
            '-XX:InitiatingHeapOccupancyPercent=20',
        ])
    else:
        args.extend([
            '-XX:G1NewSizePercent=30',
            '-XX:G1MaxNewSizePercent=40',
            '-XX:G1HeapRegionSize=8M',
            '-XX:G1ReservePercent=20',
            '-XX:InitiatingHeapOccupancyPercent=15',
        ])
    return args


def start_server():
    # Size against MemAvailable + current MC RSS so a running server does not
    # make the next start look artificially memory-starved.
    budget_mb = get_heap_budget_mb()
    heap_mb = choose_heap_mb(budget_mb)

    if screen_session_exists(SCREEN_NAME) or get_minecraft_rss_mb() > 0:
        if not stop_server():
            logging.error('Cannot start server while the old process is still running.')
            return

    jvm_args = build_jvm_args(heap_mb)
    logging.info(
        f'{budget_mb}M RAM budget; allocating {heap_mb}M heap '
        f'(Aikar flags, {"12G+" if heap_mb >= 12 * 1024 else "<12G"} profile).'
    )
    logging.info('Starting server...')
    server_dir = os.path.abspath('..')
    java_cmd = ['java', *jvm_args, '-jar', 'minecraft_server.jar', 'nogui']
    inner = f'cd {shlex.quote(server_dir)} && {shlex.join(java_cmd)}'
    os.system(f'screen -S {SCREEN_NAME} -d -m bash -c {shlex.quote(inner)}')


def ensure_server_running():
    if screen_session_exists(SCREEN_NAME) or get_minecraft_rss_mb() > 0:
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

        if screen_session_exists(SCREEN_NAME) or get_minecraft_rss_mb() > 0:
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

            if not stop_server():
                logging.error('Aborting update: could not stop the running server.')
                break

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
            logging.info('No running server; skipping shutdown/backup.')

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
