# Copyright (c) 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import stat
import shutil
import subprocess
import sys
import json
import re
import argparse

try:
    import yaml
except ImportError:
    import pip

    pip.main(["install", "PyYAML~=6.0.0"])

    import yaml

IS_WINDOWS = sys.platform.startswith("win")
VERBOSE = False

PLATFORMS_WITH_EXTERNAL_HAL = {
    "atmelsam": ["st", "atmel"],
    "chipsalliance": ["swervolf"],
    "freescalekinetis": ["st", "nxp"],
    "ststm32": ["st", "stm32"],
    "siliconlabsefm32": ["st", "silabs"],
    "nordicnrf51": ["st", "nordic"],
    "nordicnrf52": ["st", "nordic"],
    "nxplpc": ["st", "nxp"],
    "nxpimxrt": ["st", "nxp"],
    "teensy": ["st", "nxp"],
}

IGNORED_PACKAGES = {"trusted-firmware-m", "trusted-firmware-a"}


def run_cmd(args, cwd=None):
    try:
        if VERBOSE:
            print("Running command: ", args)
        subprocess.check_call(args, stderr=subprocess.STDOUT, cwd=cwd)
    except subprocess.CalledProcessError:
        return False

    return True


def to_unix_path(path):
    if not IS_WINDOWS or not path:
        return path
    return path.replace("\\", "/")


def is_commit_hash(value):
    return value and re.match(r"[0-9a-f]{7,}$", value) is not None


def is_project_required(project_config, platform_name):
    project_name = project_config["name"]
    if project_name.startswith("hal_") and project_name[
        4:
    ] not in PLATFORMS_WITH_EXTERNAL_HAL.get(platform_name, []):
        return False

    if project_config["path"].startswith("tool") or project_name.startswith("nrf_hw_"):
        return False

    return True


def prepare_package_url(remotes, default_remote_name, package_config):
    if "url" in package_config:
        remote_url = package_config["url-base"]
    else:
        remote_url = remotes.get(default_remote_name, "").get("url-base", "")
        if "remote" in package_config:
            remote_url = remotes[package_config["remote"]]["url-base"]

        remote_url = (
            remote_url
            + "/"
            + (
                package_config["repo-path"]
                if "repo-path" in package_config
                else package_config["name"]
            )
        )

    return remote_url + ".git"


def clone_repository(dst_dir, remote_url, revision, submodules_required=False):
    args = ["git", "clone"]

    is_commit = is_commit_hash(revision)
    if not is_commit:
        branch = revision
        if not branch:
            branch = "main"
            if VERBOSE:
                print(
                    "Warning! Commit hash is not specified! Using the `main` branch instead!"
                )
        args.extend(["--branch", branch, "--depth", "1"])

    if submodules_required:
        args.append("--recursive")

    if not run_cmd(args + [remote_url, dst_dir]):
        sys.stderr.write(f"Error: Failed to clone project from `{remote_url}`!\n")
        return False

    if is_commit:
        if not run_cmd(["git", "reset", "--hard", revision], cwd=dst_dir):
            return False

    return True


def install_from_remote(package_config, dst_dir, remotes, default_remote):
    remote_url = prepare_package_url(remotes, default_remote, package_config)

    if VERBOSE:
        print(f"Cloning package to `{dst_dir}`")

    assert package_config.get("revision"), "Missing project revision!"

    os.makedirs(dst_dir)
    return clone_repository(
        to_unix_path(dst_dir),
        remote_url,
        package_config["revision"],
        submodules_required=package_config.get("submodules", False),
    )


def clean_up(packages_folder):
    def _remove_readonly(func, path, _):
        # A workaround mainly for Windows to delete the ".git" folder
        os.chmod(path, stat.S_IWRITE)
        func(path)

    if VERBOSE:
        print("Installation failed. Cleaning package directory...")

    if os.path.isdir(packages_folder):
        try:
            shutil.rmtree(packages_folder, onerror=_remove_readonly)
        except OSError:
            sys.stderr.write(
                "Error: Failed to remove packages folder after failed "
                f"installation. Please remove the `{packages_folder}` folder manually!\n"
            )
            sys.exit(1)


def load_west_manifest(manifest_path):
    if not os.path.isfile(manifest_path):
        sys.stderr.write(f"Error: Couldn't find `{manifest_path}`\n")
        sys.exit(1)

    with open(manifest_path, encoding="utf8") as fp:
        try:
            return yaml.safe_load(fp).get("manifest", {})
        except yaml.YAMLError as exc:
            sys.stderr.write(f"Warning! Failed to parse `{manifest_path}`.\n")
            sys.stderr.write(str(exc) + "\n")
            sys.exit(1)


def load_state_json(state_path):
    with open(state_path, encoding="utf8") as fp:
        return json.load(fp)


def clear_deprecated_package(path, packages_folder):
    deprecated_pkg_path = os.path.join(packages_folder, path)
    if os.path.isdir(deprecated_pkg_path):
        clean_up(deprecated_pkg_path)
    else:
        print("Package path not found")


def process_bundled_projects(
    platform_name, packages_folder, west_manifest, state_manifest=None
):
    assert (
        "projects" in west_manifest
    ), "Missing the `projects` field in the package manifest!"

    # Create a folder for extra packages from west.yml
    if not os.path.isdir(packages_folder):
        os.makedirs(packages_folder)

    # If there is a state.json manifest, compare it with west.yml
    if state_manifest:
        # Convert west.yml data into a dictionary for easier comparison
        west_dep = {proj["name"]: proj for proj in west_manifest["projects"]}
        modified_projects = [
            {
                "name": name,
                "state_hash": state_hash,
                "west_revision": west_dep[name].get("revision"),
                "path": west_dep[name].get("path"),
            }
            for name, state_hash in state_manifest.items()
            if name in west_dep and state_hash != west_dep[name].get("revision")
        ]

        for project in modified_projects:
            print(
                f"The version of the `{project['name']}` package changed, "
                f"upgrading to `{project['west_revision']}`."
            )
            clear_deprecated_package(project["path"], packages_folder)

    default_remote = west_manifest.get("defaults", {}).get("remote", "")
    remotes = {remote["name"]: remote for remote in west_manifest["remotes"]}

    result = {}

    for project_config in west_manifest.get("projects", []):
        if not is_project_required(project_config, platform_name):
            continue

        project_name = project_config["name"]
        package_path = os.path.join(
            packages_folder, project_config.get("path", project_name)
        )
        if os.path.isdir(package_path):
            if VERBOSE:
                print(f"`{project_name}` is already installed!")
            result[project_name] = project_config["revision"]
            continue

        if project_name in IGNORED_PACKAGES:
            if VERBOSE:
                print(f"`{project_name}` is ignored!")
            continue

        print(f"Installing `{project_name}` project", flush=True)
        if not install_from_remote(
            project_config, package_path, remotes, default_remote
        ):
            sys.stderr.write(f"Failed to install the `{project_name}` project!\n")
            return False, result

        result[project_name] = project_config["revision"]

    return True, result


def save_state(dst_file, state_data):
    with open(dst_file, "w", encoding="utf8") as fp:
        json.dump(state_data, fp, indent=2)


def main(platform_name, secondary_installation, manifest_path):
    framework_dir = os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    packages_folder = os.path.join(framework_dir, "_pio")

    state_file = os.path.join(packages_folder, "state.json")
    if os.path.isfile(state_file) and not secondary_installation:
        if VERBOSE:
            print("The state file is present. Skipping installation.")
        sys.exit(0)

    if not shutil.which("git"):
        sys.stderr.write(
            "Error: A Git client is not installed in your system! "
            "Install a Git client from https://git-scm.com/downloads and try again.\n"
        )
        sys.exit(1)

    if not manifest_path:
        manifest_path = os.path.join(framework_dir, "west.yml")

    west_manifest = load_west_manifest(os.path.realpath(manifest_path))
    if os.path.isfile(state_file):
        state_manifest = load_state_json(state_file)
    else:
        state_manifest = None

    result, state = process_bundled_projects(
        platform_name, packages_folder, west_manifest, state_manifest
    )
    if result and state:
        save_state(state_file, state)
    elif not result:
        # Failed to install packages
        clean_up(packages_folder)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Installation script for Zephyr project dependencies"
    )
    parser.add_argument(
        "--platform", type=str, help="A dev-platform name to install packages for"
    )
    parser.add_argument(
        "--secondary-installation",
        action="store_true",
        help="Ignore the state file while checking Zephyr project dependencies",
    )
    parser.add_argument(
        "--manifest",
        type=str,
        help="Path to the west.yml manifest file",
    )
    cargs = parser.parse_args()
    try:
        main(cargs.platform, cargs.secondary_installation, cargs.manifest)
    except Exception as e:
        sys.stderr.write(
            "Error: Unknown exception occured. Failed to install bundled projects!\n"
        )
        sys.stderr.write(str(e) + "\n")
        sys.exit(1)
