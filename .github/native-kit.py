import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

workspace = pathlib.Path(os.environ['GITHUB_WORKSPACE']).resolve()
subject = workspace / 'subject'
evidence = workspace / 'evidence'
evidence.mkdir(exist_ok=True)
proof_script = workspace / 'helper/.github/native-proof.py'
mode = os.environ['MODE']
source = os.environ['SOURCE']

def proof(name):
    subprocess.run([sys.executable, str(proof_script), str(subject),
                    str(evidence / (name + '.json'))], check=True)

def run(name, args, required=True):
    proof(name + '-before')
    (evidence / (name + '-command.json')).write_text(json.dumps(args) + '\n')
    print('COMMAND', name, json.dumps(args), flush=True)
    with (evidence / (name + '.log')).open('w') as log:
        process = subprocess.Popen(args, cwd=subject, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            log.write(line)
            print(line, end='', flush=True)
        result = process.wait()
    (evidence / (name + '.exit')).write_text(str(result) + '\n')
    proof(name + '-after')
    if required and result:
        raise RuntimeError(name + ' failed, actual exit=' + str(result))
    return result

def capture(args):
    return subprocess.check_output(args, cwd=subject, text=True).strip()

def wait_http(name, url, timeout=240):
    deadline = time.monotonic() + timeout
    errors = []
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read().decode(errors='replace')
                result = {'url': url, 'status': response.status, 'body': body,
                          'errorsBeforeReady': errors}
                (evidence / (name + '-ready.json')).write_text(json.dumps(result, indent=2) + '\n')
                return
        except Exception as exc:
            errors.append(str(exc))
            time.sleep(2)
    (evidence / (name + '-readiness-failure.json')).write_text(json.dumps(errors, indent=2) + '\n')
    raise RuntimeError(name + ' never became ready')

EUREKA = 'springcloud/eureka@sha256:1c59b5c00b2df59933fe7fb39bc3ed0d31342edeb36247dfbf302af01455fc3d'
ETCD = 'gcr.io/etcd-development/etcd@sha256:59c69e2004379bb534810ae0ba2dc77226e6be2729d8312ec67323e2230005f0'
CONSUL = 'consul@sha256:96be10992ba9106ebabd67e5bee168de0274dbcc242ed05a5e66054528918558'
ZK = 'zookeeper@sha256:66a1b928d291eb6a482cadcd420a26957f0020dc464ba514421a3cb303340c83'
containers = ['kit-etcd', 'kit-consul', 'kit-zk', 'kit-eureka']

try:
    proof('00-native-entry')
    if capture(['git', 'rev-parse', 'HEAD']) != source:
        raise RuntimeError('Wrong immutable subject source')
    run('docker-version', ['docker', 'version', '--format', '{{json .}}'])
    run('docker-info', ['docker', 'info', '--format', '{{json .}}'])
    version = capture(['docker', 'version', '--format', '{{.Server.Version}}'])
    driver_status = capture(['docker', 'info', '--format', '{{json .DriverStatus}}'])
    daemon_path = capture(['which', 'dockerd'])
    daemon_hash = hashlib.sha256(pathlib.Path(daemon_path).read_bytes()).hexdigest()
    environment = {'engineVersion': version, 'driverStatus': driver_status,
                   'daemonPath': daemon_path, 'daemonSHA256Before': daemon_hash,
                   'eurekaOriginalDigest': EUREKA, 'compatibilityEnabled': False}
    (evidence / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    default_exit = run('eureka-original-default-pull', ['docker', 'pull', EUREKA], required=False)
    environment['defaultEurekaPullExit'] = default_exit
    if default_exit:
        match = re.match(r'^(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) >= (28, 2, 0):
            raise RuntimeError('Actual daemon lacks the permitted legacy compatibility range')
        if 'containerd.snapshotter' in driver_status:
            raise RuntimeError('Containerd image store is not the proved legacy distribution path')
        default_log = (evidence / 'eureka-original-default-pull.log').read_text()
        if not re.search(r'schema.?1|manifest[ .]v1|deprecated.*manifest', default_log, re.I):
            raise RuntimeError('Default failure is not the proved legacy-image problem')
        url = 'https://raw.githubusercontent.com/moby/moby/v' + version + '/distribution/pull_v2.go'
        with urllib.request.urlopen(url, timeout=30) as response:
            moby_data = response.read()
        (evidence / 'actual-moby-pull-source.go').write_bytes(moby_data)
        if b'os.Getenv("DOCKER_ENABLE_DEPRECATED_PULL_SCHEMA_1_IMAGE")' not in moby_data:
            raise RuntimeError('Exact actual daemon source lacks the documented flag')
        environment['actualMobySourceURL'] = url
        environment['actualMobySourceSHA256'] = hashlib.sha256(moby_data).hexdigest()
        configuration = '[Service]\nEnvironment="DOCKER_ENABLE_DEPRECATED_PULL_SCHEMA_1_IMAGE=1"\n'
        dropin = evidence / 'isolated-docker-compat.conf'
        dropin.write_text(configuration)
        run('compat-directory', ['sudo', 'mkdir', '-p', '/etc/systemd/system/docker.service.d'])
        run('compat-config', ['sudo', 'cp', str(dropin), '/etc/systemd/system/docker.service.d/kit-native-compat.conf'])
        run('compat-reload', ['sudo', 'systemctl', 'daemon-reload'])
        run('compat-restart', ['sudo', 'systemctl', 'restart', 'docker'])
        after_version = capture(['docker', 'version', '--format', '{{.Server.Version}}'])
        after_hash = hashlib.sha256(pathlib.Path(daemon_path).read_bytes()).hexdigest()
        if after_version != version or after_hash != daemon_hash:
            raise RuntimeError('Daemon/tool version changed instead of isolated configuration')
        environment['compatibilityEnabled'] = True
        environment['daemonSHA256After'] = after_hash
        run('eureka-original-compatible-pull', ['docker', 'pull', EUREKA])
    (evidence / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    for name, image in [('etcd', ETCD), ('consul', CONSUL), ('zk', ZK)]:
        run(name + '-original-pull', ['docker', 'pull', image])
    for name, image in [('eureka', EUREKA), ('etcd', ETCD), ('consul', CONSUL), ('zk', ZK)]:
        run(name + '-image-inspect', ['docker', 'image', 'inspect', image])
    run('etcd-start', ['docker', 'run', '-d', '--name', 'kit-etcd', '-p', '127.0.0.1:2379:2379',
                      '-e', 'ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:2379',
                      '-e', 'ETCD_ADVERTISE_CLIENT_URLS=http://0.0.0.0:2379', ETCD])
    run('consul-start', ['docker', 'run', '-d', '--name', 'kit-consul', '-p', '127.0.0.1:8500:8500', CONSUL])
    run('zk-start', ['docker', 'run', '-d', '--name', 'kit-zk', '-p', '127.0.0.1:2181:2181', ZK])
    run('eureka-start', ['docker', 'run', '-d', '--name', 'kit-eureka', '-p', '127.0.0.1:8761:8761',
                        '-e', 'eureka.server.responseCacheUpdateIntervalMs=1000', EUREKA])
    wait_http('etcd', 'http://127.0.0.1:2379/health')
    wait_http('consul', 'http://127.0.0.1:8500/v1/status/leader')
    wait_http('eureka', 'http://127.0.0.1:8761/eureka/apps')
    deadline = time.monotonic() + 180
    attempt = 0
    while True:
        attempt += 1
        result = run('zk-readiness-' + str(attempt), ['docker', 'exec', 'kit-zk', 'zkServer.sh', 'status'], required=False)
        if result == 0:
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Real ZooKeeper never became ready')
        time.sleep(2)
    proof('services-ready')
    if mode != 'environment':
        os.environ.update({'GOFLAGS': '-mod=readonly', 'ETCD_ADDR': 'http://localhost:2379',
                           'CONSUL_ADDR': 'localhost:8500', 'ZK_ADDR': 'localhost:2181',
                           'EUREKA_ADDR': 'http://localhost:8761/eureka'})
        run('go-version', ['go', 'version'])
        run('go-env', ['go', 'env', '-json'])
        run('package-resolution', ['go', 'list', '-test', '-json', '-tags', 'integration', './...'])
        if mode in ('red', 'green'):
            regression = os.environ['REGRESSION']
            if not regression:
                raise RuntimeError('Regression filter required')
            exit_code = run('regression', ['go', 'test', '-count=1', '-json', '-run', regression, './...'],
                            required=mode == 'green')
            if mode == 'red' and exit_code != 1:
                raise RuntimeError('Expected actual native RED exit1; assertions need manual evidence audit')
        if mode != 'red':
            run('full-integration', ['go', 'test', '-count=1', '-v', '-race',
                                     '-coverprofile=' + str(evidence / 'coverage.coverprofile'),
                                     '-covermode=atomic', '-tags', 'integration', './...'])
            run('vet', ['go', 'vet', '-tags', 'integration', './...'])
            tracked_go = capture(['git', 'ls-files', '*.go']).splitlines()
            run('gofmt', ['gofmt', '-l', *tracked_go])
            if (evidence / 'gofmt.log').read_text().strip():
                raise RuntimeError('Project gofmt has nonempty result; preserve baseline/final countercheck')
            run('git-diff-check', ['git', 'diff', '--check'])
    (evidence / 'native-result.json').write_text(json.dumps({'mode': mode, 'source': source,
        'fourRealServicesReady': True, 'nativeGoSuiteRun': mode != 'environment',
        'defaultEnvironmentSucceeded': default_exit == 0,
        'disclosedCompatibleEnvironment': environment['compatibilityEnabled']}, indent=2) + '\n')
finally:
    for name in containers:
        subprocess.run(['docker', 'logs', name], stdout=(evidence / (name + '-container.log')).open('w'),
                       stderr=subprocess.STDOUT)
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proof('99-final-source')
