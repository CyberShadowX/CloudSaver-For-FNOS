"""Synthetic process/HTTP tests; no NAS, cloud account, or personal data."""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from runtime import supervisor as native


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work/test-supervisor"


def temporary_case(test):
    WORK.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="case-", dir=WORK)
    root = Path(temporary.name).resolve()
    if not root.is_relative_to(WORK.resolve()):
        raise RuntimeError("Synthetic cleanup target escapes test workspace")
    test.addCleanup(temporary.cleanup)
    return root


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def wait_for_text(path: Path, expected: str, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.is_file() and expected in path.read_text(encoding="utf-8"):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Synthetic process did not reach the expected state")


def fixture_config(root, **kwargs):
    base, var = root / "base", root / "var"
    (base / "runtime").mkdir(parents=True)
    (base / "runtime/loopback.cjs").write_text("// synthetic preload", encoding="utf-8")
    for relative in ("lib/ld-musl-x86_64.so.1", "usr/local/bin/node", "app/dist-final/app.js",
                     "app/node_modules/dotenv/config.js"):
        path = var / "runtime" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic program", encoding="utf-8")
    (var / "runtime/usr/lib").mkdir(exist_ok=True)
    launcher = var / "bin" / ("node.exe" if os.name == "nt" else "node")
    launcher.parent.mkdir()
    launcher.write_text("synthetic private launcher", encoding="utf-8")
    launcher.chmod(0o755)
    frontend = var / "runtime/usr/share/nginx/html"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<title>Synthetic CloudSaver</title>", encoding="utf-8")
    return native.Config(base, var, **kwargs)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_case(self)

    def test_musl_command_and_environment_are_explicit(self):
        config = fixture_config(self.root)
        command = config.command()
        self.assertEqual(command[0], str(config.runtime / "lib/ld-musl-x86_64.so.1"))
        self.assertEqual(command[1:3], ["--library-path", os.pathsep.join([
            str(config.runtime / "usr/lib"), str(config.runtime / "lib")])])
        self.assertEqual(command[3:], [str(config.runtime / "usr/local/bin/node"), "-r",
                                      str(config.runtime / "app/node_modules/dotenv/config.js"), "-r",
                                      str(config.base / "runtime/loopback.cjs"),
                                      str(config.runtime / "app/dist-final/app.js")])
        with patch.dict(os.environ, {"NODE_OPTIONS": "--require=untrusted", "NODE_PATH": "untrusted",
                                     "JWT_SECRET": "synthetic-secret-kept-in-memory"}):
            environment = config.environment()
        self.assertNotIn("NODE_OPTIONS", environment)
        self.assertNotIn("NODE_PATH", environment)
        self.assertEqual(environment["PORT"], "18009")
        self.assertEqual(environment["CLOUDSAVER_BACKEND_PORT"], "18009")
        self.assertEqual(environment["NODE_ENV"], "production")
        self.assertEqual(environment["DOTENV_CONFIG_PATH"], str(config.runtime / "app/config/env"))
        self.assertEqual(environment["CLOUDSAVER_NODE_LAUNCHER"], str(config.node_launcher()))
        self.assertEqual(environment["JWT_SECRET"], "synthetic-secret-kept-in-memory")
        self.assertEqual(environment["NODE_TLS_REJECT_UNAUTHORIZED"], "1")

    def test_development_mode_preserves_the_existing_configuration_file(self):
        config = fixture_config(self.root, node_env="development")
        path = config.runtime / "app/config/env"
        path.parent.mkdir(parents=True)
        original = b"JWT_SECRET=synthetic-existing-value\nOTHER_SETTING=keep-existing\n"
        path.write_bytes(original)
        with patch.dict(os.environ, {"NODE_ENV": "production"}):
            environment = config.environment()
        config.command()
        self.assertEqual(environment["NODE_ENV"], "development")
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_node_environment_is_rejected(self):
        with self.assertRaises(ValueError):
            fixture_config(self.root, node_env="test")

    def test_cli_passes_only_the_selected_node_environment(self):
        config = fixture_config(self.root)
        seen = []

        async def service(arguments):
            seen.append(arguments.node_env)
            return 0

        with patch.object(native, "run_service", service):
            for mode in (None, "production", "development"):
                arguments = ["--base", str(config.base), "--var", str(config.var)]
                if mode is not None:
                    arguments.extend(("--node-env", mode))
                self.assertEqual(native.main(arguments), 0)
        self.assertEqual(seen, ["production", "production", "development"])

    def test_program_link_cannot_escape_runtime(self):
        config = fixture_config(self.root)
        node = config.runtime / "usr/local/bin/node"
        node.unlink()
        outside = self.root / "outside-node"
        outside.write_text("synthetic", encoding="utf-8")
        try:
            node.symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(ValueError):
            config.command()

    def test_missing_or_non_regular_launcher_blocks_start_preparation(self):
        config = fixture_config(self.root)
        launcher = config.node_launcher()
        launcher.unlink()
        for prepare in (config.command, config.environment):
            with self.assertRaisesRegex(ValueError, "launcher"):
                prepare()
        launcher.mkdir()
        with self.assertRaisesRegex(ValueError, "launcher"):
            config.command()

    def test_private_launcher_links_cannot_select_a_system_or_external_node(self):
        config = fixture_config(self.root)
        launcher = config.node_launcher()
        launcher.unlink()
        outside = self.root / "outside-node"
        outside.write_text("synthetic external program", encoding="utf-8")
        outside.chmod(0o755)
        try:
            launcher.symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        for prepare in (config.command, config.environment):
            with self.assertRaisesRegex(ValueError, "launcher"):
                prepare()

    def test_private_launcher_parent_cannot_link_outside_application_state(self):
        config = fixture_config(self.root)
        launcher = config.node_launcher()
        launcher.unlink()
        launcher.parent.rmdir()
        outside = self.root / "outside-bin"
        outside.mkdir()
        (outside / launcher.name).write_text("synthetic external program", encoding="utf-8")
        (outside / launcher.name).chmod(0o755)
        try:
            launcher.parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "launcher"):
            config.command()

    @unittest.skipUnless(os.name == "posix", "Executable-bit check requires POSIX")
    def test_non_executable_private_launcher_blocks_start(self):
        config = fixture_config(self.root)
        config.node_launcher().chmod(0o644)
        with self.assertRaisesRegex(ValueError, "launcher"):
            config.command()

    def test_port_collision_is_rejected(self):
        with self.assertRaises(ValueError):
            fixture_config(self.root, port=18009)

    def test_runtime_logs_reject_links(self):
        var = self.root / "var"
        var.mkdir()
        outside = self.root / "outside.log"
        outside.write_text("must-stay-unchanged", encoding="utf-8")
        try:
            (var / "logs").symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(ValueError):
            native.safe_logger(var)
        self.assertEqual(outside.read_text(encoding="utf-8"), "must-stay-unchanged")


@unittest.skipUnless(shutil.which("node"), "Local Node is required for synthetic preload checks")
class LoopbackTests(unittest.TestCase):
    def test_private_launcher_restores_fork_and_spawn_with_preloads_and_environment(self):
        root = temporary_case(self)
        config = fixture_config(root, node_env="development")
        node = Path(shutil.which("node")).resolve()
        launcher = config.node_launcher()
        if os.name == "nt":
            # Windows CreateProcess needs the private PE alias's .exe suffix;
            # the actual NAS uses the private shell launcher named bin/node.
            shutil.copyfile(node, launcher)
        else:
            launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(node)) + ' "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        self.assertNotEqual(launcher.resolve(), node)
        env_path = config.runtime / "app/config/env"
        env_path.parent.mkdir(parents=True)
        original = b"JWT_SECRET=synthetic-kept-MixedCase0123!\n"
        env_path.write_bytes(original)
        dotenv = config.runtime / "app/node_modules/dotenv/config.js"
        dotenv.write_text(r"""const fs = require('node:fs');
const match = /^JWT_SECRET=(.+)$/m.exec(fs.readFileSync(process.env.DOTENV_CONFIG_PATH, 'utf8'));
if (!match) throw new Error('Synthetic configuration unavailable');
process.env.JWT_SECRET = match[1];
global.syntheticDotenvLoaded = true;
""", encoding="utf-8")
        bad_exec = root / "simulate-loader-execpath.cjs"
        bad_exec.write_text("process.execPath=process.env.SYNTHETIC_WRONG_EXEC_PATH;\n", encoding="utf-8")
        preload = config.base / "runtime/loopback.cjs"
        preload.write_text("if (!global.syntheticDotenvLoaded) throw new Error('Preload order incorrect');\n"
                           + "require(" + json.dumps(str(ROOT / "runtime/loopback.cjs")) + ");\n",
                           encoding="utf-8")
        child_path = root / "synthetic-child.cjs"
        child_path.write_text(r"""const observed={
  kind:process.argv[2],
  privateExecPath:process.execPath===process.env.CLOUDSAVER_NODE_LAUNCHER,
  inheritedEnvironment:process.env.SYNTHETIC_CHILD_VALUE==='kept synthetic value',
  inheritedExecArgv:JSON.stringify(process.execArgv)===process.env.SYNTHETIC_PARENT_EXEC_ARGV,
  dotenvLoaded:global.syntheticDotenvLoaded===true,
  configurationAvailable:process.env.JWT_SECRET==='synthetic-kept-MixedCase0123!',
  legacyModePreserved:process.env.NODE_ENV==='development'
};
if(process.send){process.send(observed,()=>process.disconnect());}
else process.stdout.write(JSON.stringify(observed));
""", encoding="utf-8")
        parent = config.runtime / "app/dist-final/app.js"
        parent.write_text(r"""const assert=require('node:assert/strict');
const cp=require('node:child_process');
const childPath=process.env.SYNTHETIC_CHILD_PATH;
const environment={...process.env,SYNTHETIC_CHILD_VALUE:'kept synthetic value',
                   SYNTHETIC_PARENT_EXEC_ARGV:JSON.stringify(process.execArgv)};
assert.equal(process.execPath,process.env.CLOUDSAVER_NODE_LAUNCHER);
async function run(){
 const raw=cp.spawnSync(process.execPath,['-e','process.stdout.write("self-started")'],
                        {env:environment,encoding:'utf8'});
 assert.equal(raw.status,0);assert.equal(raw.stdout,'self-started');assert.equal(raw.stderr,'');
 const forked=await new Promise((resolve,reject)=>{
  const child=cp.fork(childPath,['fork synthetic argument'],{env:environment,silent:true});
  assert.equal(child.spawnfile,process.env.CLOUDSAVER_NODE_LAUNCHER);
  let observed,errors='';
  child.on('message',value=>{observed=value;});child.stderr.on('data',value=>{errors+=value;});
  child.on('error',reject);child.on('close',code=>{
   if(code!==0||errors)reject(new Error('Synthetic fork failed'));else resolve(observed);
  });
 });
 const spawned=await new Promise((resolve,reject)=>{
  const child=cp.spawn(process.execPath,[...process.execArgv,childPath,'spawn synthetic argument'],
                       {env:environment,stdio:['ignore','pipe','pipe']});
  assert.equal(child.spawnfile,process.env.CLOUDSAVER_NODE_LAUNCHER);
  let text='',errors='';child.stdout.on('data',value=>{text+=value;});
  child.stderr.on('data',value=>{errors+=value;});child.on('error',reject);
  child.on('close',code=>{
   if(code!==0||errors)reject(new Error('Synthetic spawn failed'));else resolve(JSON.parse(text));
  });
 });
 process.stdout.write(JSON.stringify({forked,spawned,rawSelfSpawn:true}));
}
run().catch(()=>{process.stderr.write('Synthetic private launcher check failed');process.exitCode=1;});
""", encoding="utf-8")
        environment = config.environment()
        environment.pop("JWT_SECRET", None)
        environment.update(SYNTHETIC_WRONG_EXEC_PATH=str(root / "not-a-node-loader"),
                           SYNTHETIC_CHILD_PATH=str(child_path))
        arguments = config.command()[4:]
        arguments[2:2] = ["-r", str(bad_exec)]
        result = subprocess.run([str(node), *arguments], env=environment,
                                cwd=config.runtime / "app", text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        expected = {"privateExecPath": True, "inheritedEnvironment": True,
                    "inheritedExecArgv": True, "dotenvLoaded": True,
                    "configurationAvailable": True, "legacyModePreserved": True}
        self.assertEqual(json.loads(result.stdout), {
            "forked": {"kind": "fork synthetic argument", **expected},
            "spawned": {"kind": "spawn synthetic argument", **expected}, "rawSelfSpawn": True,
        })
        self.assertNotIn("synthetic-kept-MixedCase", result.stdout)
        self.assertEqual(env_path.read_bytes(), original)

    def test_provided_invalid_private_launcher_fails_closed(self):
        root = temporary_case(self)
        directory = root / "directory"
        directory.mkdir()
        for launcher in ("", "relative/node", str(root / "missing-node"), str(directory)):
            with self.subTest(launcher=launcher):
                environment = os.environ.copy()
                environment.update(CLOUDSAVER_BACKEND_PORT="18009", CLOUDSAVER_NODE_LAUNCHER=launcher)
                result = subprocess.run([shutil.which("node"), "-r", str(ROOT / "runtime/loopback.cjs"), "-e", ""],
                                        env=environment, text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("A valid private native Node launcher is required", result.stderr)

    def test_branding_script_only_changes_fixed_brand_elements(self):
        script = r"""const assert = require('node:assert/strict');
class Element {
 constructor(text='', alt=null, classes=[]) {
  this.textContent=text; this.attributes={'data-keep':'untouched'};
  if (alt !== null) this.attributes.alt=alt;
  this.classList={contains:value=>classes.includes(value)};
 }
 getAttribute(name) { return this.attributes[name] ?? null; }
 setAttribute(name,value) { this.attributes[name]=value; }
 closest(selector) { assert.equal(selector,'div'); return this.container; }
 querySelector(selector) { assert.equal(selector,':scope > h1'); return this.heading; }
}
const sidebarTitle=new Element('Cloud Saver');
const container=new Element(); container.heading=sidebarTitle;
const sidebarLogo=new Element('', 'Cloud Saver Logo', ['logo__image']); sidebarLogo.container=container;
const loginLogo=new Element('', 'Cloud Saver', ['login__logo']);
const loginTitle=new Element('Cloud Saver');
const resourceTitle=new Element('Cloud Saver');
const resourceImage=new Element('', 'Cloud Saver Logo', ['resource__image']);
const anchor=(href,classes=['github-link'])=>{
 const element=new Element('GitHub',null,classes);
 if(href!==null) element.setAttribute('href',href);
 element.setAttribute('target','_blank');element.setAttribute('rel','noopener noreferrer');
 return element;
};
const linkCases=[
 ['https://github.com/jiangrui1994/cloudsaver','https://github.com/jiangrui1994/CloudSaver'],
 ['https://github.com/CyberShadowX/cLoUdSaVeR/releases/tag/V1?next=%2fcloudsaver#cloudsaver',
  'https://github.com/CyberShadowX/CloudSaver/releases/tag/V1?next=%2fcloudsaver#cloudsaver'],
 ['https://github.com/JIANGRUI1994/CLOUDSAVER/?q=cloudsaver#cloudsaver',
  'https://github.com/JIANGRUI1994/CloudSaver/?q=cloudsaver#cloudsaver'],
 ['https://github.com/CyberShadowX/cloudsaver?download=cloudsaver#readme',
  'https://github.com/CyberShadowX/CloudSaver?download=cloudsaver#readme'],
 ['https://github.com/jiangrui1994/CloudSaver','https://github.com/jiangrui1994/CloudSaver'],
 ...[
  null,'/jiangrui1994/cloudsaver','http://github.com/jiangrui1994/cloudsaver',
  'https://github.com.evil.test/jiangrui1994/cloudsaver',
  'https://github.com@evil.test/jiangrui1994/cloudsaver',
  'https://github.com/another-author/cloudsaver',
  'https://github.com/jiangrui1994/cloudsaver-extra',
  'https://github.com/jiangrui1994/cloudsaver.git',
  'https://github.com/jiangrui1994/another-project?repo=cloudsaver',
  'https://example.test/jiangrui1994/cloudsaver',
 ].map(href=>[href,href]),
];
const links=linkCases.map(([href])=>anchor(href));
const resourceHref='https://github.com/jiangrui1994/cloudsaver/issues?q=cloudsaver#resources';
const resourceLink=anchor(resourceHref,['resource__link']);
const channelLink=anchor('https://github.com/CyberShadowX/cloudsaver',['channel__link']);
const title=new Element('Cloud Saver');
const images=[sidebarLogo,loginLogo];
const logins=[loginTitle];
const allowed=new Set([sidebarLogo,sidebarTitle,loginLogo,loginTitle,title,...links]);
let observer, discover;
global.MutationObserver=class {
 constructor(callback) { this.callback=callback; this.observed=[]; observer=this; }
 disconnect() { this.observed=[]; }
 observe(element,options) {
  assert.ok(allowed.has(element), 'Observer escaped fixed branding elements');
  if(links.includes(element)) assert.deepEqual(options,{attributes:true,attributeFilter:['href']});
  this.observed.push(element);
 }
};
global.document={
 querySelectorAll(selector) {
  if (selector === 'img.logo__image, img.login__logo') return images;
  if (selector === 'h1.login__title') return logins;
  if (selector === 'a.github-link') return links;
  throw new Error('Unexpected broad selector');
 },
 querySelector(selector) { assert.equal(selector,'title'); return title; },
 get title() { return title.textContent; },
 set title(value) { title.textContent=value; }
};
global.window={setInterval(callback,delay) {assert.equal(delay,1000);discover=callback;return 1;}};
require(process.argv[1]);
assert.equal(sidebarTitle.textContent,'CloudSaver');
assert.equal(loginTitle.textContent,'CloudSaver');
assert.equal(sidebarLogo.getAttribute('alt'),'CloudSaver Logo');
assert.equal(loginLogo.getAttribute('alt'),'CloudSaver');
assert.equal(document.title,'CloudSaver');
assert.equal(resourceTitle.textContent,'Cloud Saver');
assert.equal(resourceImage.getAttribute('alt'),'Cloud Saver Logo');
for(let index=0;index<linkCases.length;index++) {
 const [original,expected]=linkCases[index];
 const attributes={'data-keep':'untouched',target:'_blank',rel:'noopener noreferrer'};
 if(original!==null) attributes.href=expected;
 assert.deepEqual(links[index].attributes,attributes);
 assert.equal(links[index].textContent,'GitHub');
}
assert.equal(resourceLink.getAttribute('href'),resourceHref);
assert.equal(channelLink.getAttribute('href'),'https://github.com/CyberShadowX/cloudsaver');
for (const heading of [sidebarTitle,loginTitle]) assert.deepEqual(heading.attributes,{'data-keep':'untouched'});
sidebarTitle.textContent='Cloud Saver'; sidebarLogo.setAttribute('alt','Cloud Saver Logo');
links[0].setAttribute('href','https://github.com/jiangrui1994/cloudsaver/issues?state=open#top');
observer.callback();
assert.equal(sidebarTitle.textContent,'CloudSaver');
assert.equal(sidebarLogo.getAttribute('alt'),'CloudSaver Logo');
assert.equal(links[0].getAttribute('href'),'https://github.com/jiangrui1994/CloudSaver/issues?state=open#top');
const remountedTitle=new Element('Cloud Saver');
const remountedContainer=new Element();remountedContainer.heading=remountedTitle;
const remountedLogo=new Element('', 'Cloud Saver Logo', ['logo__image']);remountedLogo.container=remountedContainer;
const remountedLink=anchor('https://github.com/jiangrui1994/cloudsaver/releases?tag=old#assets');
allowed.add(remountedLink);links.splice(0,links.length,remountedLink);
allowed.add(remountedTitle);allowed.add(remountedLogo);images.push(remountedLogo);discover();
assert.equal(remountedTitle.textContent,'CloudSaver');
assert.equal(remountedLogo.getAttribute('alt'),'CloudSaver Logo');
assert.equal(remountedLink.getAttribute('href'),'https://github.com/jiangrui1994/CloudSaver/releases?tag=old#assets');
sidebarTitle.textContent='Cloud Saver resource';sidebarLogo.setAttribute('alt','Cloud Saver illustration');
document.title='Cloud Saver resource';observer.callback();
assert.equal(sidebarTitle.textContent,'Cloud Saver resource');
assert.equal(sidebarLogo.getAttribute('alt'),'Cloud Saver illustration');
assert.equal(document.title,'Cloud Saver resource');
assert.equal(resourceTitle.textContent,'Cloud Saver');
assert.equal(resourceImage.getAttribute('alt'),'Cloud Saver Logo');
assert.equal(resourceLink.getAttribute('href'),resourceHref);
assert.equal(channelLink.getAttribute('href'),'https://github.com/CyberShadowX/cloudsaver');
console.log(JSON.stringify({fixedBrands:true,resourcePreserved:true,remountHandled:true,scopedObservers:true,
                           githubCase:true,urlPartsPreserved:true,linkRemountHandled:true}));
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(ROOT / "runtime/branding.js")],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "fixedBrands": True, "resourcePreserved": True, "remountHandled": True, "scopedObservers": True,
            "githubCase": True, "urlPartsPreserved": True, "linkRemountHandled": True,
        })

    def test_dotenv_is_loaded_before_loopback_and_early_production_validation(self):
        root = temporary_case(self)
        config = fixture_config(root)
        env_path = config.runtime / "app/config/env"
        env_path.parent.mkdir(parents=True)
        original = b"JWT_SECRET=synthetic-existing-value-with-MixedCase0123!\n"
        env_path.write_bytes(original)
        dotenv = config.runtime / "app/node_modules/dotenv/config.js"
        dotenv.write_text(r"""const fs = require('node:fs');
const match = /^JWT_SECRET=(.+)$/m.exec(fs.readFileSync(process.env.DOTENV_CONFIG_PATH, 'utf8'));
if (!match) throw new Error('Synthetic configuration unavailable');
process.env.JWT_SECRET = match[1];
global.syntheticDotenvLoaded = true;
""", encoding="utf-8")
        preload = config.base / "runtime/loopback.cjs"
        preload.write_text("if (!global.syntheticDotenvLoaded) throw new Error('Preload order incorrect');\n"
                           + "require(" + json.dumps(str(ROOT / "runtime/loopback.cjs")) + ");\n",
                           encoding="utf-8")
        (config.runtime / "app/dist-final/app.js").write_text(r"""const value = process.env.JWT_SECRET || '';
if (!/[a-z]/.test(value) || !/[A-Z]/.test(value) || !/[0-9]/.test(value) || !/[^A-Za-z0-9]/.test(value)) {
  throw new Error('Synthetic early production guard failed');
}
console.log(JSON.stringify({guardSatisfied:true, nodeEnv:process.env.NODE_ENV}));
""", encoding="utf-8")
        environment = config.environment()
        environment.pop("JWT_SECRET", None)
        result = subprocess.run([shutil.which("node"), *config.command()[4:]], env=environment,
                                cwd=config.runtime / "app", text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"guardSatisfied": True, "nodeEnv": "production"})
        self.assertEqual(result.stderr, "")
        self.assertNotIn("synthetic-existing-value", result.stdout)
        self.assertEqual(env_path.read_bytes(), original)

    def test_only_the_selected_numeric_and_options_listener_are_constrained(self):
        preload = ROOT / "runtime/loopback.cjs"
        for style in ("number", "options", "string"):
            with self.subTest(style=style):
                port = unused_port()
                script = r"""
const net = require('node:net');
const selected = net.createServer();
const other = net.createServer();
const options = {port: Number(process.env.CLOUDSAVER_BACKEND_PORT), host: '0.0.0.0'};
let ready = 0;
const done = () => {
 if (++ready !== 2) return;
 console.log(JSON.stringify({selected: selected.address().address, other: other.address().address,
                             originalHost: options.host}));
 selected.close(); other.close();
};
other.listen({port:0, host:'0.0.0.0'}, done);
if (process.argv[1] === 'options') selected.listen(options, done);
else if (process.argv[1] === 'string') selected.listen(String(options.port), '0.0.0.0', done);
else selected.listen(options.port, done);
"""
                environment = os.environ.copy()
                environment["CLOUDSAVER_BACKEND_PORT"] = str(port)
                result = subprocess.run([shutil.which("node"), "-r", str(preload), "-e", script, style],
                                        env=environment, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = json.loads(result.stdout)
                self.assertEqual(observed, {"selected": "127.0.0.1", "other": "0.0.0.0",
                                            "originalHost": "0.0.0.0"})

    def test_invalid_backend_port_fails_closed(self):
        environment = os.environ.copy()
        environment["CLOUDSAVER_BACKEND_PORT"] = "0"
        result = subprocess.run([shutil.which("node"), "-r", str(ROOT / "runtime/loopback.cjs"), "-e", ""],
                                env=environment, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("A valid native backend port is required", result.stderr)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_case(self)
        (self.root / "321/fd").mkdir(parents=True)
        (self.root / "321/fd/5").write_text("", encoding="ascii")
        (self.root / "net").mkdir()

    def table(self, address="0100007F", inode="123", ipv6=False):
        local = f"{address}:4659"  # 18009
        line = f"0: {local} 00000000:0000 0A 0:0 00:0 0 1000 0 {inode} 1"
        (self.root / "net/tcp").write_text("header\n" + ("" if ipv6 else line), encoding="ascii")
        (self.root / "net/tcp6").write_text("header\n" + (line if ipv6 else ""), encoding="ascii")

    def test_ownership_requires_loopback_socket_from_this_pid(self):
        self.table()
        with patch.object(native.os, "readlink", return_value="socket:[123]"):
            self.assertTrue(native.linux_listener_owned(321, 18009, self.root))
        for address, inode in (("00000000", "123"), ("0100007F", "999")):
            self.table(address, inode)
            with patch.object(native.os, "readlink", return_value="socket:[123]"):
                self.assertFalse(native.linux_listener_owned(321, 18009, self.root))

    def test_ipv6_or_malformed_listener_cannot_substitute_for_private_backend(self):
        self.table("0" * 32, ipv6=True)
        with patch.object(native.os, "readlink", return_value="socket:[123]"):
            self.assertFalse(native.linux_listener_owned(321, 18009, self.root))
        (self.root / "net/tcp").write_text("header\nmalformed", encoding="ascii")
        self.assertFalse(native.linux_listener_owned(321, 18009, self.root))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = temporary_case(self)
        self.frontend = self.root / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text("<title>Synthetic page</title>", encoding="utf-8")
        (self.frontend / "app.js").write_text("console.log('synthetic');", encoding="utf-8")
        (self.frontend / "manifest.webmanifest").write_text("{}", encoding="utf-8")
        (self.frontend / ".private").write_text("must not serve", encoding="utf-8")
        self.log_output = io.StringIO()
        self.logger = logging.Logger("synthetic.gateway")
        self.logger.addHandler(logging.StreamHandler(self.log_output))
        self.stream_gate = asyncio.Event()
        self.seen = []
        backend = web.Application()

        async def handler(request):
            self.seen.append(request.path)
            if request.path == "/events":
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b"data: first\n\n")
                await asyncio.wait_for(self.stream_gate.wait(), 5)
                await response.write(b"data: second\n\n")
                await response.write_eof()
                return response
            if request.path == "/cookies":
                response = web.Response(text="ok")
                response.headers.add("Set-Cookie", "first=synthetic-a; HttpOnly")
                response.headers.add("Set-Cookie", "second=synthetic-b; HttpOnly")
                return response
            if request.path == "/redirect":
                return web.Response(status=302, headers={"Location": "https://example.invalid/not-followed"})
            if request.path == "/denied":
                return web.Response(status=401, text="synthetic-private-upstream-body")
            if request.path.startswith("/ws-redirect/"):
                return web.Response(status=int(request.path.rsplit("/", 1)[1]),
                                    headers={"Location": self.redirect_target})
            if request.path in ("/ws", "/ws-close"):
                connection = web.WebSocketResponse(protocols=("synthetic-test",), autoping=False)
                await connection.prepare(request)
                if request.path == "/ws-close":
                    await connection.close(code=1008, message=b"synthetic policy")
                    return connection
                async for message in connection:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await connection.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await connection.send_bytes(message.data)
                    elif message.type == aiohttp.WSMsgType.PING:
                        await connection.pong(message.data)
                return connection
            body = await request.read()
            response = web.json_response({
                "path": request.path, "raw_query": request.rel_url.raw_query_string,
                "body": body.hex(), "authorization": request.headers.get("Authorization"),
                "cookie": request.headers.get("Cookie"), "leaked_hop": request.headers.get("X-Leak"),
                "host": request.headers.get("Host"), "real_ip": request.headers.get("X-Real-IP"),
                "encoding": request.headers.get("Content-Encoding"),
            })
            response.headers["Connection"] = "X-Upstream-Hop"
            response.headers["X-Upstream-Hop"] = "must-not-forward"
            return response

        backend.router.add_route("*", "/{tail:.*}", handler)
        self.backend = TestServer(backend)
        await self.backend.start_server(auto_decompress=False, access_log=None)
        application = native.create_app(self.frontend, self.backend.port, self.logger)
        self.server = TestServer(application)
        await self.server.start_server(auto_decompress=False, access_log=None)
        self.client = TestClient(self.server, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()

    async def asyncTearDown(self):
        self.stream_gate.set()
        await self.client.close()
        await self.backend.close()

    async def test_api_preserves_method_body_query_auth_and_filters_hop_headers(self):
        target = URL(str(self.server.make_url("/api/echo")) + "?a=%2B%2F&same=one&same=two", encoded=True)
        response = await self.client.session.post(
            target, data=b"synthetic payload",
            headers={"Authorization": "Bearer synthetic-secret", "Cookie": "sid=synthetic-secret",
                     "Host": "synthetic.example:8008", "Connection": "X-Leak, keep-alive", "X-Leak": "private"},
        )
        observed = await response.json()
        self.assertEqual(observed["path"], "/echo")
        self.assertEqual(observed["raw_query"], "a=%2B%2F&same=one&same=two")
        self.assertEqual(bytes.fromhex(observed["body"]), b"synthetic payload")
        self.assertEqual(observed["authorization"], "Bearer synthetic-secret")
        self.assertEqual(observed["cookie"], "sid=synthetic-secret")
        self.assertEqual(observed["host"], "synthetic.example:8008")
        self.assertIsNone(observed["leaked_hop"])
        self.assertNotIn("X-Upstream-Hop", response.headers)
        self.assertNotIn("synthetic-secret", self.log_output.getvalue())

    async def test_request_encoding_is_not_silently_changed(self):
        body = gzip.compress(b"synthetic compressed body")
        response = await self.client.post("/api/echo", data=body, headers={"Content-Encoding": "gzip"})
        observed = await response.json()
        self.assertEqual(bytes.fromhex(observed["body"]), body)
        self.assertEqual(observed["encoding"], "gzip")

    async def test_response_streaming_does_not_wait_for_completion(self):
        response = await self.client.get("/api/events")
        first = await asyncio.wait_for(response.content.readline(), 1)
        self.assertEqual(first, b"data: first\n")
        self.stream_gate.set()
        self.assertIn(b"data: second", await response.read())

    async def test_multiple_cookies_are_preserved_but_not_saved_between_users(self):
        response = await self.client.get("/api/cookies")
        self.assertEqual(len(response.headers.getall("Set-Cookie")), 2)
        await response.read()
        echo = await self.client.get("/api/echo")
        self.assertIsNone((await echo.json())["cookie"])

    async def test_backend_redirect_is_not_followed(self):
        response = await self.client.get("/api/redirect", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "https://example.invalid/not-followed")
        await response.read()

    async def test_original_image_and_socketio_paths_are_preserved(self):
        for path in ("/tele-images/synthetic", "/socket.io/polling"):
            response = await self.client.get(path)
            self.assertEqual((await response.json())["path"], path)

    async def test_static_spa_head_manifest_and_method_boundary(self):
        response = await self.client.get("/resources/page")
        self.assertIn("Synthetic page", await response.text())
        head = await self.client.head("/app.js")
        self.assertEqual(head.status, 200)
        self.assertEqual(await head.read(), b"")
        manifest = await self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.headers["Content-Type"], "application/manifest+json")
        await manifest.read()
        missing = await self.client.get("/missing.js")
        self.assertEqual(missing.status, 404)
        await missing.read()
        wrong_method = await self.client.post("/resources/page", data=b"unused")
        self.assertEqual(wrong_method.status, 405)
        await wrong_method.read()

    async def test_index_injection_is_response_only_and_preserves_resource_text(self):
        original = b'<html><head><title>Cloud Saver</title></head><body><h1>Cloud Saver resource</h1></body></html>'
        path = self.frontend / "index.html"
        path.write_bytes(original)
        for route in ("/", "/index.html", "/resources/page"):
            response = await self.client.get(route)
            body = await response.read()
            self.assertEqual(response.content_type, "text/html")
            self.assertEqual(body.count(native.BRANDING_SCRIPT_TAG), 1)
            self.assertEqual(body.replace(native.BRANDING_SCRIPT_TAG, b""), original)
        self.assertEqual(path.read_bytes(), original)
        javascript = b'const resourceTitle="Cloud Saver";'
        (self.frontend / "app.js").write_bytes(javascript)
        response = await self.client.get("/app.js")
        self.assertEqual(await response.read(), javascript)
        api_body = b'{"channel":"Cloud Saver","resource":"Cloud Saver"}'
        response = await self.client.post("/api/echo", data=api_body)
        self.assertEqual(bytes.fromhex((await response.json())["body"]), api_body)

    async def test_branding_script_has_fixed_path_mime_and_no_filesystem_path(self):
        response = await self.client.get(native.BRANDING_SCRIPT_URL)
        body = await response.read()
        self.assertEqual(response.content_type, "application/javascript")
        self.assertEqual(body, (ROOT / "runtime/branding.js").read_bytes())
        self.assertNotIn(str(ROOT).encode(), body)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        head = await self.client.head(native.BRANDING_SCRIPT_URL)
        self.assertEqual(head.status, 200)
        self.assertEqual(await head.read(), b"")
        rejected = await self.client.post(native.BRANDING_SCRIPT_URL, data=b"unused")
        self.assertEqual(rejected.status, 405)
        await rejected.read()

    async def test_index_adaptation_requires_head_small_size_and_the_actual_index(self):
        cases = (
            ("missing-head", b"<title>Cloud Saver</title>"),
            ("size-boundary", b"<head></head>" + b"x" * (native.MAX_BRANDING_HTML - len(b"<head></head>"))),
        )
        for label, original in cases:
            with self.subTest(case=label):
                (self.frontend / "index.html").write_bytes(original)
                response = await self.client.get("/")
                body = await response.read()
                self.assertTrue(body == original)
                self.assertNotIn(native.BRANDING_SCRIPT_TAG, body)
        ordinary = b"<html><head></head><body>Cloud Saver resource</body></html>"
        (self.frontend / "resource.html").write_bytes(ordinary)
        response = await self.client.get("/resource.html")
        self.assertEqual(await response.read(), ordinary)

    async def test_manifest_changes_only_exact_brand_fields_without_writing_source(self):
        path = self.frontend / "manifest.webmanifest"
        manifest = {
            "name": "Cloud Saver", "short_name": "Cloud Saver", "start_url": "/", "display": "standalone",
            "description": "Cloud Saver resource", "icons": [{"src": "/logo.png", "sizes": "192x192"}],
        }
        original = json.dumps(manifest).encode()
        path.write_bytes(original)
        response = await self.client.get("/manifest.webmanifest")
        self.assertEqual(response.content_type, "application/manifest+json")
        adapted = await response.json()
        expected = dict(manifest, name="CloudSaver", short_name="CloudSaver")
        self.assertEqual(adapted, expected)
        self.assertEqual(path.read_bytes(), original)
        for unchanged in (b'{"name":"Cloud Saver resource","short_name":"My Library"}', b"not-json"):
            path.write_bytes(unchanged)
            response = await self.client.get("/manifest.webmanifest")
            self.assertEqual(await response.read(), unchanged)

    async def test_hidden_encoded_traversal_and_outside_links_are_refused(self):
        for path in ("/.private", "/%2e%2e/private", "/%252e%252e/private", "/..%5cprivate", "/%00"):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode("ascii"))
            await writer.drain()
            status = await reader.readline()
            writer.close()
            await writer.wait_closed()
            self.assertEqual(status.split()[1], b"404", path)
        outside = self.root / "outside.txt"
        outside.write_text("must not serve", encoding="utf-8")
        try:
            (self.frontend / "outside.txt").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        response = await self.client.get("/outside.txt")
        self.assertEqual(response.status, 404)
        self.assertNotIn("must not serve", await response.text())

    async def test_websocket_text_binary_ping_and_protocol_roundtrip(self):
        connection = await self.client.ws_connect("/api/ws", protocols=("synthetic-test",), autoping=False)
        self.assertEqual(connection.protocol, "synthetic-test")
        await connection.send_str("synthetic text")
        self.assertEqual((await connection.receive(timeout=2)).data, "synthetic text")
        await connection.send_bytes(b"synthetic bytes")
        self.assertEqual((await connection.receive(timeout=2)).data, b"synthetic bytes")
        await connection.ping(b"synthetic ping")
        pong = await connection.receive(timeout=2)
        self.assertEqual(pong.type, aiohttp.WSMsgType.PONG)
        self.assertEqual(pong.data, b"synthetic ping")
        await connection.close()

    async def test_precompressed_static_link_cannot_escape_frontend(self):
        outside = self.root / "private.gz"
        outside.write_bytes(gzip.compress(b"synthetic must not serve"))
        try:
            (self.frontend / "app.js.gz").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable")
        response = await self.client.get("/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.status, 404)
        await response.read()

    async def test_websocket_close_code_and_reason_are_forwarded(self):
        connection = await self.client.ws_connect("/api/ws-close", protocols=("synthetic-test",))
        message = await connection.receive(timeout=2)
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(message.data, 1008)
        self.assertEqual(message.extra, "synthetic policy")
        await connection.close()

    async def test_failed_websocket_auth_preserves_status_without_upstream_body(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
            await self.client.ws_connect("/api/denied")
        self.assertEqual(failure.exception.status, 401)
        self.assertNotIn("synthetic-private-upstream-body", self.log_output.getvalue())

    async def test_websocket_redirect_never_visits_another_host_or_carries_client_headers(self):
        visited = []
        destination_app = web.Application()

        async def capture(request):
            visited.append(dict(request.headers))
            connection = web.WebSocketResponse()
            await connection.prepare(request)
            await connection.close()
            return connection

        destination_app.router.add_route("*", "/{tail:.*}", capture)
        destination = TestServer(destination_app)
        await destination.start_server(access_log=None)
        try:
            self.redirect_target = str(destination.make_url("/capture")).replace("127.0.0.1", "localhost")
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status):
                    with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
                        await self.client.ws_connect(f"/api/ws-redirect/{status}", headers={
                            "Authorization": "Bearer synthetic-client-private",
                            "Cookie": "session=synthetic-client-private",
                            "X-Private-Token": "synthetic-custom-private",
                        })
                    self.assertEqual(failure.exception.status, 502)
                    self.assertEqual(visited, [])
            self.assertNotIn("synthetic-client-private", self.log_output.getvalue())
            self.assertNotIn("synthetic-custom-private", self.log_output.getvalue())
        finally:
            await destination.close()


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = temporary_case(self)
        self.backend = None

    async def asyncTearDown(self):
        if self.backend is not None and self.backend.returncode is None:
            self.backend.process.kill()
            await self.backend.wait()

    async def spawn(self, config):
        script = """import http.server,sys
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'synthetic backend';self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
http.server.ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
"""
        options = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(config.backend_port),
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        self.backend = native.ManagedBackend(process, native.process_start_time(process.pid))
        return self.backend

    def config(self, **kwargs):
        port, backend_port = unused_port(), unused_port()
        while backend_port == port:
            backend_port = unused_port()
        return fixture_config(self.root, port=port, backend_port=backend_port, host="127.0.0.1",
                              startup_timeout=5, shutdown_timeout=2, **kwargs)

    def owns_listener(self, pid, port):
        return os.name != "posix" or native.linux_listener_owned(pid, port)

    async def assert_port_closed(self, port):
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_normal_stop_closes_frontend_and_backend_and_records_actual_cleanup(self):
        config = self.config()
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(native.run_service(config, spawn_backend=self.spawn,
            listener_check=self.owns_listener, shutdown_event=stop, ready_callback=ready.set))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            # Constructor mistakes in older aiohttp used to announce ready,
            # then fail only on the first accepted connection. Fail promptly
            # here instead of hanging until the client's five-minute default.
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(f"http://127.0.0.1:{config.port}/api/health") as response:
                    self.assertEqual(await response.text(), "synthetic backend")
            stop.set()
            self.assertEqual(await asyncio.wait_for(service, 5), 0)
            self.assertIsNotNone(self.backend.returncode)
            receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["clean"])
            self.assertEqual(receipt["backend_pid"], self.backend.pid)
            await self.assert_port_closed(config.port)
        finally:
            stop.set()
            await service

    async def test_real_frontend_protocol_accepts_shutdown_configuration(self):
        # This uses the installed aiohttp classes and a real socket. Under 3.8
        # an unsupported AppRunner kwarg fails only when a connection is
        # accepted, even though runner.setup/site.start have both succeeded.
        app = web.Application()

        async def response(request):
            return web.Response(text="synthetic protocol accepted")

        app.router.add_get("/", response)
        runner_shutdown, site_shutdown = native._frontend_shutdown_options()
        runner = web.AppRunner(app, access_log=None, auto_decompress=False, **runner_shutdown)
        try:
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, **site_shutdown)
            await site.start()
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                async with session.get(f"http://127.0.0.1:{port}/") as actual:
                    self.assertEqual(actual.status, 200)
                    self.assertEqual(await actual.text(), "synthetic protocol accepted")
            # Verify where the real version consumed the five-second setting,
            # rather than only comparing our generated keyword dictionary.
            timeout = getattr(runner, "_shutdown_timeout", None)
            if timeout is None:
                timeout = site._shutdown_timeout
            self.assertEqual(timeout, 5.0)
        finally:
            await runner.cleanup()

    async def test_frontend_cleanup_error_never_records_clean_shutdown(self):
        config = self.config()
        stop, ready = asyncio.Event(), asyncio.Event()
        cleanup = web.AppRunner.cleanup

        async def fail_after_cleanup(runner):
            await cleanup(runner)
            raise RuntimeError("Synthetic frontend cleanup failure")

        with patch.object(web.AppRunner, "cleanup", fail_after_cleanup):
            service = asyncio.create_task(native.run_service(config, spawn_backend=self.spawn,
                listener_check=self.owns_listener, shutdown_event=stop, ready_callback=ready.set))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                stop.set()
                self.assertEqual(await asyncio.wait_for(service, 5), 1)
                receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
                self.assertFalse(receipt["clean"])
                self.assertIsNotNone(self.backend.returncode)
                await self.assert_port_closed(config.port)
            finally:
                stop.set()
                await asyncio.gather(service, return_exceptions=True)

    async def test_backend_exit_stops_frontend_and_reports_failure(self):
        config = self.config()
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(native.run_service(config, spawn_backend=self.spawn,
            listener_check=self.owns_listener, shutdown_event=stop, ready_callback=ready.set))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            self.backend.process.kill()
            self.assertEqual(await asyncio.wait_for(service, 5), 1)
            await self.assert_port_closed(config.port)
        finally:
            stop.set()
            await service

    async def test_a_connected_port_without_ownership_is_never_published(self):
        config = self.config()
        object.__setattr__(config, "startup_timeout", 0.2)
        ready = asyncio.Event()
        result = await native.run_service(config, spawn_backend=self.spawn,
            listener_check=lambda pid, port: False, shutdown_event=asyncio.Event(), ready_callback=ready.set)
        self.assertEqual(result, 1)
        self.assertFalse(ready.is_set())
        self.assertIsNotNone(self.backend.returncode)
        await self.assert_port_closed(config.port)

    async def test_errors_do_not_print_environment_or_paths_to_console(self):
        config = self.config()
        (config.runtime / "usr/local/bin/node").unlink()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"JWT_SECRET": "synthetic-secret-not-for-logs"}), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            result = await native.run_service(config, shutdown_event=asyncio.Event())
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        log = (config.var / "logs/service.log").read_text(encoding="utf-8")
        self.assertIn("service_failed", log)
        self.assertNotIn("synthetic-secret-not-for-logs", log)
        self.assertNotIn(str(config.runtime), log)

    async def test_signal_handlers_remain_active_until_pending_backend_really_stops(self):
        config = self.config()
        object.__setattr__(config, "shutdown_timeout", 0.05)
        ready = asyncio.Event()
        handlers, removed = {}, []
        loop = asyncio.get_running_loop()

        def install(sig, callback, *arguments):
            handlers[sig] = lambda: callback(*arguments)

        def remove(sig):
            removed.append(sig)
            return handlers.pop(sig, None) is not None

        with patch.object(loop, "add_signal_handler", install), \
                patch.object(loop, "remove_signal_handler", remove), \
                patch.object(native.ManagedBackend, "terminate", lambda backend: None):
            service = asyncio.create_task(native.run_service(config, spawn_backend=self.spawn,
                listener_check=self.owns_listener, ready_callback=ready.set))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                handlers[signal.SIGTERM]()
                await wait_for_text(config.var / "logs/service.log", "backend_stop_pending")
                self.assertEqual(removed, [])
                self.assertIn(signal.SIGTERM, handlers)
                self.assertIn(signal.SIGINT, handlers)
                handlers[signal.SIGTERM]()
                handlers[signal.SIGINT]()
                await asyncio.sleep(0.02)
                self.assertFalse(service.done())
                receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
                self.assertFalse(receipt["clean"])
                self.backend.process.terminate()
                self.assertEqual(await asyncio.wait_for(service, 5), 0)
                self.assertEqual(handlers, {})
                self.assertEqual(set(removed), {signal.SIGTERM, signal.SIGINT})
                receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
                self.assertTrue(receipt["clean"])
            finally:
                if self.backend is not None and self.backend.returncode is None:
                    self.backend.process.kill()
                    await self.backend.wait()
                await asyncio.gather(service, return_exceptions=True)


@unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir() and hasattr(signal, "SIGUSR1"),
                     "Actual repeated TERM regression requires Linux process signals")
class RepeatedSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_term_does_not_kill_guardian_for_a_term_ignoring_backend(self):
        root = temporary_case(self)
        port, backend_port = unused_port(), unused_port()
        while port == backend_port:
            backend_port = unused_port()
        config = fixture_config(root, port=port, backend_port=backend_port, host="127.0.0.1")
        helper = root / "synthetic_guardian.py"
        helper.write_text('''import asyncio,sys,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from runtime import supervisor as native
program="""import http.server,signal,sys
signal.signal(signal.SIGTERM,signal.SIG_IGN)
def done(*args):raise SystemExit(0)
signal.signal(signal.SIGUSR1,done)
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):self.send_response(200);self.end_headers()
 def log_message(self,*args):pass
http.server.ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
"""
async def spawn(config):
 process=await asyncio.create_subprocess_exec(sys.executable,'-c',program,str(config.backend_port),
  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
 return native.ManagedBackend(process,native.process_start_time(process.pid))
config=native.Config(Path(sys.argv[2]),Path(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5]),
 host='127.0.0.1',startup_timeout=10,shutdown_timeout=0.1)
result=asyncio.run(native.run_service(config,spawn_backend=spawn,
 ready_callback=lambda:(config.var/'ready').write_text('ready',encoding='utf-8')))
raise SystemExit(result)
''', encoding="utf-8")
        guardian = await asyncio.create_subprocess_exec(
            sys.executable, str(helper), str(ROOT), str(config.base), str(config.var), str(port), str(backend_port),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        backend_pid = backend_start = None
        try:
            await wait_for_text(config.var / "ready", "ready", timeout=10)
            receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["supervisor_pid"], guardian.pid)
            backend_pid, backend_start = receipt["backend_pid"], receipt["backend_start_time"]
            self.assertIsNotNone(backend_start)
            os.kill(guardian.pid, signal.SIGTERM)
            await wait_for_text(config.var / "logs/service.log", "backend_stop_pending")
            os.kill(guardian.pid, signal.SIGTERM)
            await asyncio.sleep(0.1)
            self.assertIsNone(guardian.returncode)
            self.assertEqual(native.process_start_time(backend_pid), backend_start)
            receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["clean"])
            os.kill(backend_pid, signal.SIGUSR1)
            self.assertEqual(await asyncio.wait_for(guardian.wait(), 5), 0)
            receipt = json.loads((config.var / "run/shutdown.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["clean"])
        finally:
            # Only synthetic processes created by this test are signalled;
            # verify the saved Linux start identity before touching the backend.
            if (backend_pid is not None and backend_start is not None
                    and native.process_start_time(backend_pid) == backend_start):
                try:
                    os.kill(backend_pid, signal.SIGUSR1)
                except ProcessLookupError:
                    pass
            if guardian.returncode is None:
                try:
                    await asyncio.wait_for(guardian.wait(), 3)
                except asyncio.TimeoutError:
                    guardian.kill()
                    await guardian.wait()

if __name__ == "__main__":
    unittest.main()
