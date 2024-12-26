# Copyright 2014-present PlatformIO <contact@platformio.org>
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

import json
import os
import subprocess
import shutil
import sys
import re

import click
import semantic_version

from SCons.Script import ARGUMENTS, Builder, COMMAND_LINE_TARGETS

from platformio import fs
from platformio.compat import IS_WINDOWS
from platformio.proc import exec_command
from platformio.package import version

Import("env")

try:
    import yaml
except ImportError:
    env.Execute("$PYTHONEXE -m pip install PyYAML~=6.0.0")

    import yaml


platform = env.PioPlatform()
board = env.BoardConfig()

FRAMEWORK_DIR = platform.get_package_dir("framework-zephyr")
FRAMEWORK_VERSION = platform.get_package_version("framework-zephyr")
assert os.path.isdir(FRAMEWORK_DIR)

BUILD_DIR = env.subst("$BUILD_DIR")
PROJECT_DIR = env.subst("$PROJECT_DIR")
PROJECT_SRC_DIR = env.subst("$PROJECT_SRC_DIR")
CMAKE_API_DIR = os.path.join(BUILD_DIR, ".cmake", "api", "v1")
CMAKE_API_QUERY_DIR = os.path.join(CMAKE_API_DIR, "query")
CMAKE_API_REPLY_DIR = os.path.join(CMAKE_API_DIR, "reply")

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

ZEPHYR_ENV_VERSION = "1.0.0"
ZEPHYR_APP_BUILD_CONTROL = board.get("build.zephyr.app_build_control", False)
ZEPHYR_PRESERVE_OBJ_EXT = board.get("build.zephyr.preserve_obj_file_ext", False)


def get_board_architecture(board_config):
    if board_config.get("build.cpu", "").lower().startswith("cortex"):
        return "arm"
    elif board_config.get("build.march", "").startswith(("rv64", "rv32")):
        return "riscv"
    elif board_config.get("build.mcu") == "esp32":
        return "xtensa32"

    sys.stderr.write(
        "Error: Cannot configure Zephyr environment for %s\n"
        % env.subst("$PIOPLATFORM")
    )
    env.Exit(1)


def populate_zephyr_env_vars(zephyr_env, board_config):
    toolchain_variant = "UNKNOWN"
    arch = get_board_architecture(board_config)
    if arch == "arm":
        toolchain_variant = "gnuarmemb"
        zephyr_env["GNUARMEMB_TOOLCHAIN_PATH"] = platform.get_package_dir(
            "toolchain-gccarmnoneeabi"
        )
    elif arch == "riscv":
        toolchain_variant = "cross-compile"
        zephyr_env["CROSS_COMPILE"] = os.path.join(
            platform.get_package_dir("toolchain-riscv"),
            "bin",
            "riscv64-unknown-elf-",
        )
    elif arch == "xtensa32":
        toolchain_variant = "espressif"
        zephyr_env["ESPRESSIF_TOOLCHAIN_PATH"] = platform.get_package_dir(
            "toolchain-xtensa32"
        )

    zephyr_env["ZEPHYR_TOOLCHAIN_VARIANT"] = toolchain_variant
    zephyr_env["ZEPHYR_BASE"] = FRAMEWORK_DIR

    additional_packages = [
        platform.get_package_dir("tool-dtc"),
        platform.get_package_dir("tool-ninja"),
    ]

    if not IS_WINDOWS:
        additional_packages.append(platform.get_package_dir("tool-gperf"))

    zephyr_env["PATH"] = os.pathsep.join(additional_packages)


def is_proper_zephyr_project():
    return os.path.isfile(os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt"))


def create_default_project_files():
    cmake_tpl = """cmake_minimum_required(VERSION 3.13.1)
include($ENV{ZEPHYR_BASE}/cmake/app/boilerplate.cmake NO_POLICY_SCOPE)
project(%s)

FILE(GLOB app_sources ../src/*.c*)
target_sources(app PRIVATE ${app_sources})
"""

    app_tpl = """#include <zephyr/kernel.h>

void main(void)
{
}
"""

    cmake_txt_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt")
    if not os.path.isfile(cmake_txt_file):
        if not os.path.isdir(os.path.dirname(cmake_txt_file)):
            os.makedirs(os.path.dirname(cmake_txt_file))
        with open(cmake_txt_file, "w") as fp:
            fp.write(cmake_tpl % os.path.basename(PROJECT_DIR))

    project_conf_file = os.path.join(PROJECT_DIR, "zephyr", "prj.conf")
    if not os.path.isfile(project_conf_file):
        open(project_conf_file, "a").close()  # create an empty file

    if not os.listdir(os.path.join(PROJECT_SRC_DIR)):
        # create an empty file to make CMake happy during first init
        with open(os.path.join(PROJECT_SRC_DIR, "main.c"), "w") as fp:
            fp.write(app_tpl)


def is_cmake_reconfigure_required():
    cmake_cache_file = os.path.join(BUILD_DIR, "CMakeCache.txt")
    cmake_txt_file = os.path.join(PROJECT_DIR, "zephyr", "CMakeLists.txt")
    cmake_preconf_dir = os.path.join(
        BUILD_DIR, "zephyr", "include", "generated", "zephyr"
    )
    cmake_preconf_misc = os.path.join(BUILD_DIR, "zephyr", "misc", "generated")
    zephyr_prj_conf = os.path.join(PROJECT_DIR, "zephyr", "prj.conf")

    for d in (CMAKE_API_REPLY_DIR, cmake_preconf_dir, cmake_preconf_misc):
        if not os.path.isdir(d) or not os.listdir(d):
            return True
    if not os.path.isfile(cmake_cache_file):
        return True
    if not os.path.isfile(os.path.join(BUILD_DIR, "build.ninja")):
        return True
    if os.path.getmtime(cmake_txt_file) > os.path.getmtime(cmake_cache_file):
        return True
    if os.path.isfile(zephyr_prj_conf) and os.path.getmtime(
        zephyr_prj_conf
    ) > os.path.getmtime(cmake_cache_file):
        return True
    if os.path.getmtime(FRAMEWORK_DIR) > os.path.getmtime(cmake_cache_file):
        return True

    return False


def run_cmake(manifest):
    print("Reading CMake configuration")

    CONFIG_PATH = board.get(
        "build.zephyr.config_path",
        os.path.join(PROJECT_DIR, "config.%s" % env.subst("$PIOENV")),
    )

    python_executable = get_python_exe()
    cmake_cmd = [
        os.path.join(
            platform.get_package_dir("tool-cmake") or "", "bin", "cmake"
        ),
        "-S",
        os.path.join(PROJECT_DIR, "zephyr"),
        "-B",
        BUILD_DIR,
        "-G",
        "Ninja",
        "-DBOARD=%s" % get_zephyr_target(board),
        "-DPYTHON_EXECUTABLE:FILEPATH=%s" % python_executable,
        "-DPython3_EXECUTABLE:FILEPATH=%s" % python_executable,
        "-DPIO_PACKAGES_DIR:PATH=%s" % env.subst("$PROJECT_PACKAGES_DIR"),
        "-DDOTCONFIG=" + CONFIG_PATH,
        "-DBUILD_VERSION=zephyr-v" + FRAMEWORK_VERSION.split(".")[1],
    ]

    menuconfig_file = os.path.join(PROJECT_DIR, "zephyr", "menuconfig.conf")
    if os.path.isfile(menuconfig_file):
        print("Adding -DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)
        cmake_cmd.append("-DOVERLAY_CONFIG:FILEPATH=%s" % menuconfig_file)

    if board.get("build.zephyr.cmake_extra_args", ""):
        cmake_cmd.extend(
            click.parser.split_arg_string(
                board.get("build.zephyr.cmake_extra_args")
            )
        )

    modules = [generate_default_component()]

    for project in manifest.get("projects", []):
        if not is_project_required(project):
            continue

        modules.append(
            fs.to_unix_path(
                os.path.join(
                    FRAMEWORK_DIR,
                    "_pio",
                    project["path"] if "path" in project else project["name"],
                )
            )
        )

    cmake_cmd.extend(["-D", "ZEPHYR_MODULES=" + ";".join(modules)])

    # Run Zephyr in an isolated environment with specific env vars
    zephyr_env = os.environ.copy()
    populate_zephyr_env_vars(zephyr_env, board)

    result = exec_command(cmake_cmd, env=zephyr_env)
    if result["returncode"] != 0:
        sys.stderr.write(result["out"] + "\n")
        sys.stderr.write(result["err"])
        env.Exit(1)

    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        print(result["out"])
        print(result["err"])


def get_cmake_code_model(manifest):
    if not is_proper_zephyr_project():
        create_default_project_files()

    if is_cmake_reconfigure_required():
        # Explicitly clean build folder to avoid cached values
        if os.path.isdir(CMAKE_API_DIR):
            fs.rmtree(BUILD_DIR)
        query_file = os.path.join(CMAKE_API_QUERY_DIR, "codemodel-v2")
        if not os.path.isfile(query_file):
            os.makedirs(os.path.dirname(query_file))
            open(query_file, "a").close()  # create an empty file
        run_cmake(manifest)

    if not os.path.isdir(CMAKE_API_REPLY_DIR) or not os.listdir(
        CMAKE_API_REPLY_DIR
    ):
        sys.stderr.write("Error: Couldn't find CMake API response file\n")
        env.Exit(1)

    codemodel = {}
    for target in os.listdir(CMAKE_API_REPLY_DIR):
        if target.startswith("codemodel-v2"):
            with open(os.path.join(CMAKE_API_REPLY_DIR, target), "r") as fp:
                codemodel = json.load(fp)

    assert codemodel["version"]["major"] == 2
    return codemodel


def get_zephyr_target(board_config):
    return board_config.get("build.zephyr.variant", env.subst("$BOARD").lower())


def get_target_elf_arch(board_config):
    architecture = get_board_architecture(board_config)
    if architecture == "arm":
        return "elf32-littlearm"
    if architecture == "riscv":
        if board.get("build.march", "") == "rv32":
            return "elf32-littleriscv"
        return "elf64-littleriscv"
    if architecture == "xtensa32":
        return "elf32-xtensa-le"

    sys.stderr.write(
        "Error: Cannot find correct elf architecture for %s\n"
        % env.subst("$PIOPLATFORM")
    )
    env.Exit(1)


def build_library(default_env, lib_config, project_src_dir, prepend_dir=None):
    lib_name = lib_config.get("nameOnDisk", lib_config["name"])

    lib_path = lib_config["paths"]["build"]
    if prepend_dir:
        lib_path = os.path.join(prepend_dir, lib_path)

    # Special case for libraries with relative path
    if lib_path == ".":
        # Try to extract the path from the first artifact entry
        lib_path = os.path.dirname(
            lib_config.get("artifacts", [{}])[0].get("path", "")
        )

    lib_objects = compile_source_files(
        lib_config, default_env, project_src_dir, prepend_dir
    )

    return default_env.Library(
        target=os.path.join("$BUILD_DIR", lib_path, lib_name),
        source=lib_objects,
    )


def get_target_config(project_configs, target_index):
    target_json = project_configs.get("targets")[target_index].get(
        "jsonFile", ""
    )
    target_config_file = os.path.join(CMAKE_API_REPLY_DIR, target_json)
    if not os.path.isfile(target_config_file):
        sys.stderr.write(
            "Error: Couldn't find target config %s\n" % target_json
        )
        env.Exit(1)

    with open(target_config_file) as fp:
        return json.load(fp)


def _fix_package_path(module_path):
    # Possible package names in 'package@version' format is not compatible with CMake
    module_name = os.path.basename(module_path)
    if "@" in module_name:
        new_path = os.path.join(
            os.path.dirname(module_path),
            module_name.replace("@", "-"),
        )
        os.rename(module_path, new_path)
        module_path = new_path

    assert module_path and os.path.isdir(module_path)
    return module_path


def generate_includible_file(source_file):
    cmd = [
        get_python_exe(),
        '"%s"' % os.path.join(FRAMEWORK_DIR, "scripts", "build", "file2hex.py"),
        "--file",
        "$SOURCE",
        ">",
        "$TARGET",
    ]

    return env.Command(
        os.path.join(
            "$BUILD_DIR", "zephyr", "include", "generated", "${SOURCE.file}.inc"
        ),
        env.File(source_file),
        env.VerboseAction(" ".join(cmd), "Generating file $TARGET"),
    )


def generate_kobject_files():
    kobj_files = (
        os.path.join(
            "$BUILD_DIR", "zephyr", "include", "generated", "zephyr", f
        )
        for f in ("kobj-types-enum.h", "otype-to-str.h", "otype-to-size.h")
    )

    if all(os.path.isfile(env.subst(f)) for f in kobj_files):
        return

    cmd = (
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR, "scripts", "build", "gen_kobject_list.py"
        ),
        "--kobj-types-output",
        os.path.join(
            BUILD_DIR,
            "zephyr",
            "include",
            "generated",
            "zephyr",
            "kobj-types-enum.h",
        ),
        "--kobj-otype-output",
        os.path.join(
            BUILD_DIR,
            "zephyr",
            "include",
            "generated",
            "zephyr",
            "otype-to-str.h",
        ),
        "--kobj-size-output",
        os.path.join(
            BUILD_DIR,
            "zephyr",
            "include",
            "generated",
            "zephyr",
            "otype-to-size.h",
        ),
        "--include-subsystem-list",
        os.path.join(
            BUILD_DIR, "zephyr", "misc", "generated", "struct_tags.json"
        ),
    )

    env.Execute(env.VerboseAction(" ".join(cmd), "Generating KObject files"))


def generate_strerror_table(project_settings):
    strerror_header = os.path.join(
        BUILD_DIR,
        "zephyr",
        "include",
        "generated",
        "libc",
        "minimal",
        "strerror_table.h",
    )

    if os.path.isfile(env.subst(strerror_header)):
        return

    cmd = (
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR, "scripts", "build", "gen_strerror_table.py"
        ),
        "-i",
        os.path.join(
            FRAMEWORK_DIR, "lib", "libc", "minimal", "include", "errno.h"
        ),
        "-o",
        strerror_header,
    )

    env.Execute(env.VerboseAction(" ".join(cmd), "Generating strerror table"))


def generate_strsignal_table():
    strsignal_table_header = os.path.join(
        BUILD_DIR,
        "zephyr",
        "include",
        "generated",
        "posix",
        "strsignal_table.h",
    )

    if os.path.isfile(env.subst(strsignal_table_header)):
        return

    cmd = (
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR, "scripts", "build", "gen_strsignal_table.py"
        ),
        "-i",
        os.path.join(FRAMEWORK_DIR, "include", "zephyr", "posix", "signal.h"),
        "-o",
        strsignal_table_header,
    )

    env.Execute(env.VerboseAction(" ".join(cmd), "Generating strsignal table"))


def get_boot_signature_key_file(project_settings):
    signature_key_file = board.get(
        "build.zephyr.bootloader.signature_key_file",
        project_settings.get("CONFIG_MCUBOOT_SIGNATURE_KEY_FILE", ""),
    )

    if not os.path.isabs(signature_key_file) and not os.path.isfile(
        signature_key_file
    ):
        print(
            "Warning: MCUboot signature key is not specified! "
            "The default `root-rsa-2048.pem` will be used!"
        )

        signature_key_file = os.path.join(
            FRAMEWORK_DIR, "_pio", "bootloader", "mcuboot", "root-rsa-2048.pem"
        )

    if not os.path.isfile(signature_key_file):
        print(
            "Warning: Cannot find the `%s` signature key!" % signature_key_file
        )

    return signature_key_file


def generate_pubkey(key_file):
    if not os.path.isfile(key_file):
        key_file = os.path.join(
            FRAMEWORK_DIR, "_pio", "bootloader", "mcuboot", key_file
        )

    assert os.path.isfile(
        key_file
    ), f"Missing `{key_file}` key file for signing MCUboot images"

    generated_pubkey = os.path.join(
        BUILD_DIR,
        "zephyr",
        "autogen-pubkey.c",
    )

    if os.path.isfile(env.subst(generated_pubkey)):
        return

    cmd = (
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR,
            "_pio",
            "bootloader",
            "mcuboot",
            "scripts",
            "imgtool.py",
        ),
        "getpub",
        "-k",
        key_file,
        ">",
        generated_pubkey,
    )

    env.Execute(
        env.VerboseAction(" ".join(cmd), "Generating public MCUboot key")
    )


def generate_version_header():
    version_header = os.path.join(
        BUILD_DIR, "zephyr", "include", "generated", "zephyr", "version.h"
    )

    if os.path.isfile(env.subst(version_header)):
        return

    cmd = (
        os.path.join(
            platform.get_package_dir("tool-cmake") or "", "bin", "cmake"
        ),
        "-DZEPHYR_BASE=%s" % FRAMEWORK_DIR,
        "-DVERSION_TYPE=KERNEL",
        "-DVERSION_FILE=" + os.path.join(FRAMEWORK_DIR, "VERSION"),
        "-DBUILD_VERSION=zephyr-v" + FRAMEWORK_VERSION.split(".")[1],
        "-DKERNEL_VERSION_CUSTOMIZATION="
        + board.get("build.zephyr.kernel_version_customization", ""),
        "-DOUT_FILE=%s" % version_header,
        "-P",
        os.path.join(FRAMEWORK_DIR, "cmake", "gen_version_h.cmake"),
    )

    if env.Execute(
        env.VerboseAction(" ".join(cmd), "Generating version header file")
    ):
        # A problem occurred while making the temp directory.
        sys.stderr.write("Error: Couldn't generate version header file\n")
        env.Exit(1)


def validate_driver():

    driver_header = os.path.join(
        BUILD_DIR,
        "zephyr",
        "include",
        "generated",
        "zephyr",
        "driver-validation.h",
    )

    if os.path.isfile(env.subst(driver_header)):
        return

    cmd = (
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR, "scripts", "build", "gen_kobject_list.py"
        ),
        "--validation-output",
        driver_header,
        "--include-subsystem-list",
        os.path.join(
            "$BUILD_DIR", "zephyr", "misc", "generated", "struct_tags.json"
        ),
    )

    env.Execute(env.VerboseAction(" ".join(cmd), "Validating driver"))


def generate_dev_handles(preliminary_elf_path, project_settings):
    cmd = [
        get_python_exe(),
        '"%s"'
        % os.path.join(FRAMEWORK_DIR, "scripts", "build", "gen_device_deps.py"),
        "--output-source",
        "$TARGET",
        "--kernel",
        "$SOURCE",
        "--start-symbol",
        "__device_start",
        "--zephyr-base",
        FRAMEWORK_DIR,
    ]

    if project_settings.get(
        "CONFIG_DEVICE_DEPS", False
    ) and project_settings.get("CONFIG_DEVICE_DEPS_DYNAMIC"):
        cmd.append("--dynamic-deps")

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", "device_deps.c"),
        preliminary_elf_path,
        env.VerboseAction(" ".join(cmd), "Generating $TARGET"),
    )


def parse_syscalls():
    syscalls_config = os.path.join(
        BUILD_DIR, "zephyr", "misc", "generated", "syscalls.json"
    )

    struct_tags = os.path.join(
        BUILD_DIR, "zephyr", "misc", "generated", "struct_tags.json"
    )

    syscalls_file_list = os.path.join(
        BUILD_DIR, "zephyr", "misc", "generated", "syscalls_file_list.txt"
    )

    if not all(
        os.path.isfile(env.subst(f))
        for f in (syscalls_config, struct_tags, syscalls_file_list)
    ):
        cmd = [
            get_python_exe(),
            '"%s"'
            % os.path.join(
                FRAMEWORK_DIR, "scripts", "build", "parse_syscalls.py"
            ),
            "--scan",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "include"),
            "--scan",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "drivers"),
            "--scan",
            '"%s"' % os.path.join(FRAMEWORK_DIR, "subsys", "net"),
            "--file-list",
            syscalls_file_list,
        ]

        # Temporarily until CMake exports actual custom commands
        if board.get("build.zephyr.syscall_include_dirs", ""):
            incs = [
                inc if os.path.isabs(inc) else os.path.join(PROJECT_DIR, inc)
                for inc in board.get(
                    "build.zephyr.syscall_include_dirs"
                ).split()
            ]

            cmd.extend(['--scan "%s"' % inc for inc in incs])

        cmd.extend(
            ("--json-file", syscalls_config, "--tag-struct-file", struct_tags)
        )

        env.Execute(env.VerboseAction(" ".join(cmd), "Parsing system calls"))

    return syscalls_config


def generate_syscall_files(syscalls_json, project_settings):
    syscalls_header = os.path.join(
        BUILD_DIR, "zephyr", "include", "generated", "zephyr", "syscall_list.h"
    )

    if os.path.isfile(syscalls_header):
        return

    cmd = [
        get_python_exe(),
        '"%s"'
        % os.path.join(FRAMEWORK_DIR, "scripts", "build", "gen_syscalls.py"),
        "--json-file",
        syscalls_json,
        "--base-output",
        os.path.join(
            "$BUILD_DIR", "zephyr", "include", "generated", "zephyr", "syscalls"
        ),
        "--syscall-dispatch",
        os.path.join(
            "$BUILD_DIR",
            "zephyr",
            "include",
            "generated",
            "zephyr",
            "syscall_dispatch.c",
        ),
        "--syscall-list",
        syscalls_header,
    ]

    if project_settings.get("CONFIG_64BIT", False) == "y":
        cmd.append("--long-registers")

    if project_settings.get("CONFIG_TIMEOUT_64BIT", False) == "y":
        cmd.extend(("--split-type", "k_timeout_t", "--split-type", "k_ticks_t"))

    env.Execute(env.VerboseAction(" ".join(cmd), "Generating syscall files"))


def find_base_ldscript(app_includes):
    # A temporary solution since there is no easy way to find linker script
    for inc in app_includes["plain_includes"]:
        if not os.path.isdir(inc):
            continue
        for f in os.listdir(inc):
            if f == "linker.ld" and os.path.isfile(os.path.join(inc, f)):
                return os.path.join(inc, f)

    sys.stderr.write("Error: Couldn't find a base linker script!\n")
    env.Exit(1)


def get_linkerscript_cmd(
    app_includes,
    script_name,
    project_settings,
    cmake_settings,
    extra_flags=None,
):
    ldscript = project_settings.get(
        "CONFIG_CUSTOM_LINKER_SCRIPT",
        cmake_settings.get(
            "BOARD_LINKER_SCRIPT",
            cmake_settings.get("SOC_LINKER_SCRIPT"),
        ),
    )

    if not ldscript:
        # Final try for a linker script somewhere in include paths
        ldscript = find_base_ldscript(app_includes)

    if not os.path.isabs(ldscript):
        ldscript = os.path.join(PROJECT_DIR, "zephyr", ldscript)

    cmd = [
        "$CC",
        "-x",
        "assembler-with-cpp",
        "-undef",
        "-MD",
        "-MF",
        "${TARGET}.dep",
        "-MT",
        "$TARGET",
        "-D_LINKER",
        "-D_ASMLANGUAGE",
        "-imacros",
        os.path.join(
            "$BUILD_DIR",
            "zephyr",
            "include",
            "generated",
            "zephyr",
            "autoconf.h",
        ),
        "-D__GCC_LINKER_CMD__",
        "-E",
        "$SOURCE",
        "-P",
        "-o",
        "$TARGET",
    ]

    if extra_flags:
        cmd.extend(extra_flags)

    cmd.extend(['-I"%s"' % inc for inc in app_includes["plain_includes"]])

    return env.Command(
        os.path.join("$BUILD_DIR", "zephyr", script_name),
        ldscript,
        env.VerboseAction(" ".join(cmd), "Generating linker script $TARGET"),
    )


def load_target_configurations(cmake_codemodel):
    configs = {}
    project_configs = cmake_codemodel.get("configurations")[0]
    for config in project_configs.get("projects", []):
        for target_index in config.get("targetIndexes", []):
            target_config = get_target_config(project_configs, target_index)
            configs[target_config["name"]] = target_config

    return configs


def extract_defines_from_compile_group(compile_group):
    result = []
    result.extend(
        [
            d.get("define").replace('"', '\\"').strip()
            for d in compile_group.get("defines", [])
        ]
    )

    for f in compile_group.get("compileCommandFragments", []):
        result.extend(
            env.ParseFlags(f.get("fragment", "")).get("CPPDEFINES", [])
        )
    return result


def prepare_build_envs(config, default_env):
    build_envs = []
    target_compile_groups = config.get("compileGroups", [])

    for cg in target_compile_groups:
        includes = extract_includes_from_compile_group(
            cg, path_prefix=FRAMEWORK_DIR
        )
        defines = extract_defines_from_compile_group(cg)
        build_env = default_env.Clone()
        compile_commands = cg.get("compileCommandFragments", [])

        i = 0
        length = len(compile_commands)
        while i < length:
            build_flags = compile_commands[i].get("fragment", "")
            if build_flags.strip() in ("-imacros", "-include"):
                i += 1
                file_path = compile_commands[i].get("fragment", "")
                build_env.Append(CCFLAGS=[build_flags + file_path])
            elif build_flags.strip() and not build_flags.startswith("-D"):
                build_env.AppendUnique(**build_env.ParseFlags(build_flags))
            i += 1
        build_env.AppendUnique(
            CPPDEFINES=defines, CPPPATH=includes["plain_includes"]
        )
        if includes["prefixed_includes"]:
            build_env.Append(
                CCFLAGS=["-iprefix", fs.to_unix_path(FRAMEWORK_DIR)]
            )
            build_env.Append(
                CCFLAGS=[
                    "-iwithprefixbefore/" + inc
                    for inc in includes["prefixed_includes"]
                ]
            )
        if includes["sys_includes"]:
            build_env.Append(
                CCFLAGS=["-isystem" + inc for inc in includes["sys_includes"]]
            )
        build_env.Append(ASPPFLAGS=build_env.get("CCFLAGS", [])[:])
        build_env.ProcessUnFlags(default_env.get("BUILD_UNFLAGS"))
        if "debug" in env.GetBuildType():
            build_env.ConfigureDebugFlags()
        build_envs.append(build_env)

    return build_envs


def compile_source_files(
    config, default_env, project_src_dir, prepend_dir=None
):
    build_envs = prepare_build_envs(config, default_env)
    objects = []
    for source in config.get("sources", []):
        if source["path"].endswith(".rule"):
            continue
        compile_group_idx = source.get("compileGroupIndex")
        if compile_group_idx is not None:
            src_path = source.get("path")
            if not os.path.isabs(src_path):
                # For cases when sources are located near CMakeLists.txt
                src_path = os.path.join(PROJECT_DIR, "zephyr", src_path)
            local_path = config["paths"]["source"]
            if not os.path.isabs(local_path):
                local_path = os.path.join(
                    project_src_dir, config["paths"]["source"]
                )
            obj_path_temp = os.path.join(
                "$BUILD_DIR",
                prepend_dir or config["name"].replace("framework-zephyr", ""),
                config["paths"]["build"],
            )
            if src_path.startswith(local_path):
                obj_path = os.path.join(
                    obj_path_temp, os.path.relpath(src_path, local_path)
                )
            else:
                obj_path = os.path.join(
                    obj_path_temp, os.path.basename(src_path)
                )

            objects.append(
                build_envs[compile_group_idx].StaticObject(
                    target=os.path.join(
                        obj_path
                        + (".o" if not ZEPHYR_PRESERVE_OBJ_EXT else ".obj")
                    ),
                    source=os.path.realpath(src_path),
                )
            )

    return objects


def get_app_includes(app_config):
    includes = extract_includes_from_compile_group(
        app_config["compileGroups"][0]
    )
    return includes


def extract_includes_from_compile_group(compile_group, path_prefix=None):
    def _normalize_prefix(prefix):
        prefix = fs.to_unix_path(prefix)
        if not prefix.endswith("/"):
            prefix = prefix + "/"
        return prefix

    if path_prefix:
        path_prefix = _normalize_prefix(path_prefix)

    includes = []
    sys_includes = []
    prefixed_includes = []
    for inc in compile_group.get("includes", []):
        inc_path = fs.to_unix_path(inc["path"])
        if inc.get("isSystem", False):
            sys_includes.append(inc_path)
        elif path_prefix and inc_path.startswith(path_prefix):
            prefixed_includes.append(
                fs.to_unix_path(os.path.relpath(inc_path, path_prefix))
            )
        else:
            includes.append(inc_path)

    return {
        "plain_includes": includes,
        "sys_includes": sys_includes,
        "prefixed_includes": prefixed_includes,
    }


def get_app_defines(app_config):
    return extract_defines_from_compile_group(app_config["compileGroups"][0])


def extract_link_args(target_config):
    link_args = {
        "link_flags": [],
        "lib_paths": [],
        "project_libs": {
            "whole_libs": [],
            "generic_libs": [],
            "standard_libs": [],
        },
    }

    is_whole_archive = False
    for f in target_config.get("link", {}).get("commandFragments", []):
        fragment = f.get("fragment", "").strip().replace("\\", "/")
        fragment_role = f.get("role", "").strip()
        if not fragment or not fragment_role:
            continue
        args = click.parser.split_arg_string(fragment)
        if "-Wl,--whole-archive" in fragment:
            is_whole_archive = True
        if "-Wl,--no-whole-archive" in fragment:
            is_whole_archive = False
        if fragment_role == "flags":
            link_args["link_flags"].extend(args)
        elif fragment_role == "libraries":
            if fragment.startswith(("-l", "-Wl,-l")):
                link_args["project_libs"]["standard_libs"].extend(args)
            elif fragment.startswith("-L"):
                lib_path = fragment.replace("-L", "").strip()
                if lib_path not in link_args["lib_paths"]:
                    link_args["lib_paths"].append(lib_path.replace('"', ""))
            elif fragment.startswith("-") and not fragment.startswith("-l"):
                # CMake mistakenly marks link_flags as libraries
                link_args["link_flags"].extend(args)
            elif os.path.isfile(fragment) and os.path.isabs(fragment):
                # In case of precompiled archives from framework package
                lib_path = os.path.dirname(fragment)
                if lib_path not in link_args["lib_paths"]:
                    link_args["lib_paths"].append(os.path.dirname(fragment))
                link_args["project_libs"]["standard_libs"].extend(
                    [
                        os.path.basename(lib)
                        for lib in args
                        if lib.endswith(".a")
                    ]
                )
            elif fragment.endswith(".a"):
                link_args["project_libs"][
                    "whole_libs" if is_whole_archive else "generic_libs"
                ].extend(
                    [
                        lib.replace("\\", "/")
                        for lib in args
                        if lib.endswith(".a")
                    ]
                )
            else:
                link_args["link_flags"].extend(args)

    return link_args


def generate_isr_table_file_cmd(
    preliminary_elf, board_config, project_settings
):
    cmd = [
        get_python_exe(),
        '"%s"'
        % os.path.join(FRAMEWORK_DIR, "scripts", "build", "gen_isr_tables.py"),
        "--output-source",
        "${TARGETS[0]}",
        "--linker-output-files",
        "${TARGETS[1]}",
        "${TARGETS[2]}",
        "--kernel",
        "$SOURCE",
        "--intlist-section",
        ".intList",
        "--intlist-section",
        "intList",
    ]

    if project_settings.get("CONFIG_GEN_ISR_TABLES", "") == "y":
        cmd.append("--sw-isr-table")
    if project_settings.get("CONFIG_GEN_IRQ_VECTOR_TABLE", "") == "y":
        cmd.append("--vector-table")

    cmd = env.Command(
        [
            os.path.join("$BUILD_DIR", "zephyr", "isr_tables.c"),
            os.path.join("$BUILD_DIR", "zephyr", "isr_tables_vt.ld"),
            os.path.join("$BUILD_DIR", "zephyr", "isr_tables_swi.ld"),
        ],
        preliminary_elf,
        env.VerboseAction(" ".join(cmd), "Generating ISR table $TARGET"),
    )

    return cmd


def generate_offset_header_file_cmd():
    cmd = [
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR, "scripts", "build", "gen_offset_header.py"
        ),
        "-i",
        "$SOURCE",
        "-o",
        "$TARGET",
    ]

    return env.Command(
        os.path.join(
            "$BUILD_DIR",
            "zephyr",
            "include",
            "generated",
            "zephyr",
            "offsets.h",
        ),
        os.path.join(
            "$BUILD_DIR",
            "offsets",
            "zephyr",
            "arch",
            get_board_architecture(board),
            "core",
            "offsets",
            "offsets.c" + (".o" if not ZEPHYR_PRESERVE_OBJ_EXT else ".obj"),
        ),
        env.VerboseAction(
            " ".join(cmd), "Generating header file with offsets $TARGET"
        ),
    )


def generate_relocation_files_cmd():
    def _extract_relocations_arg(ninja_buildfile):
        assert os.path.isfile(
            ninja_buildfile
        ), "Cannot extract relocation command! Ninja build file is missing!"

        with open(ninja_buildfile, encoding="utf8") as fp:
            args_pattern = r"COMMAND = [\S\s]*gen_relocate_app\.py\s+-d.*-i\s+\"(?P<args>.*)\"\s+-o[\S\s]*DESC"
            regex_match = re.search(args_pattern, fp.read())
            assert (
                regex_match
            ), "Cannot extract relocation command! Ninja build file is missing!"

            return regex_match.group("args")

    # Note: the gen_relocate_app.py script traverses the build directory in order
    # to find corresponding object files (.obj) specified in its arguments list
    ninja_buildfile = os.path.join(BUILD_DIR, "build.ninja")
    cmd = (
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR, "scripts", "build", "gen_relocate_app.py"
        ),
        "-d",
        "$BUILD_DIR",
        "-i",
        '"%s"' % _extract_relocations_arg(ninja_buildfile),
        "-o",
        "${TARGETS[0]}",
        "-s",
        "${TARGETS[1]}",
        "-b",
        "${TARGETS[2]}",
        "-c",
        "${TARGETS[3]}",
        "--default_ram_region",
        "RAM",
    )

    return env.Command(
        [
            os.path.join(
                "$BUILD_DIR",
                "zephyr",
                "include",
                "generated",
                "linker_relocate.ld",
            ),
            os.path.join(
                "$BUILD_DIR",
                "zephyr",
                "include",
                "generated",
                "linker_sram_data_relocate.ld",
            ),
            os.path.join(
                "$BUILD_DIR",
                "zephyr",
                "include",
                "generated",
                "linker_sram_bss_relocate.ld",
            ),
            os.path.join("$BUILD_DIR", "zephyr", "code_relocation.c"),
        ],
        ninja_buildfile,
        env.VerboseAction(" ".join(cmd), "Generating relocation files"),
    )


def filter_args(args, allowed, ignore=None):
    if not allowed:
        return []

    ignore = ignore or []
    result = []
    i = 0
    length = len(args)
    while i < length:
        if any(args[i].startswith(f) for f in allowed) and not any(
            args[i].startswith(f) for f in ignore
        ):
            result.append(args[i])
            if (
                i + 1 < length
                and not args[i + 1].startswith("-")
                and not args[i + 1].endswith((".o", ".obj"))
            ):
                i += 1
                result.append(args[i])
        i += 1
    return result


def load_project_settings():
    result = {}
    config_re = re.compile(r"^([^#=]+)=(.+)$")
    config_file = os.path.join(BUILD_DIR, "zephyr", ".config")
    if not os.path.isfile(config_file):
        print("Warning! Missing project configuration file `%s`" % config_file)
        return {}

    with open(config_file) as f:
        for line in f:
            re_match = config_re.match(line)
            if re_match:
                config_value = re_match.group(2)
                if config_value.startswith('"') and config_value.endswith('"'):
                    config_value = config_value[1:-1]
                result[re_match.group(1)] = config_value

    return result


def load_cmake_settings():
    result = {}
    config_re = re.compile(r"^([^#=\/]+):(.*)?=(.+)$")
    cmake_cache_file = os.path.join(BUILD_DIR, "CMakeCache.txt")
    if not os.path.isfile(cmake_cache_file):
        print("Warning! Missing `CMakeCache.txt` file `%s`" % cmake_cache_file)
        return {}

    with open(cmake_cache_file) as f:
        for line in f:
            if line.startswith(("#", "/", "_", "check_")):
                continue
            re_match = config_re.match(line)
            if re_match:
                config_value = re_match.group(3)
                if config_value.startswith('"') and config_value.endswith('"'):
                    config_value = config_value[1:-1]
                result[re_match.group(1)] = config_value

    return result


def RunMenuconfig(target, source, env):
    zephyr_env = os.environ.copy()
    populate_zephyr_env_vars(zephyr_env, board)

    rc = subprocess.call(
        [
            os.path.join(
                platform.get_package_dir("tool-cmake"), "bin", "cmake"
            ),
            "--build",
            BUILD_DIR,
            "--target",
            "menuconfig",
        ],
        env=zephyr_env,
    )

    if rc != 0:
        sys.stderr.write("Error: Couldn't execute 'menuconfig' target.\n")
        env.Exit(1)


def get_project_lib_deps(modules_map, main_config):
    def _collect_lib_deps(config, libs=None):
        libs = libs or {}
        deps = config.get("dependencies", [])
        if not deps:
            return []

        for d in config["dependencies"]:
            dependency_id = d["id"]
            if not modules_map.get(dependency_id, {}):
                continue
            if dependency_id not in libs:
                libs[dependency_id] = modules_map[dependency_id]
                _collect_lib_deps(libs[dependency_id]["config"], libs)

        return libs

    return _collect_lib_deps(main_config)


def load_west_manifest():

    project_manifest_path = os.path.join(PROJECT_DIR, "west.yml")
    framework_manifest_path = os.path.join(FRAMEWORK_DIR, "west.yml")

    if os.path.isfile(project_manifest_path):
        print("Info: Using a project-based dependency manifest file.")
        manifest_path = project_manifest_path
    elif os.path.isfile(framework_manifest_path):
        manifest_path = framework_manifest_path
    else:
        sys.stderr.write(
            "Error: Couldn't find `%s`\n" % framework_manifest_path)
        env.Exit(1)

    with open(manifest_path) as fp:
        try:
            return yaml.safe_load(fp).get("manifest", {}), manifest_path
        except yaml.YAMLError as e:
            sys.stderr.write("Warning! Failed to parse `%s`.\n" % manifest_path)
            sys.stderr.write(str(e) + "\n")
            env.Exit(1)


def generate_default_component():
    # Used to force CMake generate build environments for all supported languages

    prj_cmake_tpl = """# Warning! Do not delete this auto-generated file.
file(GLOB module_srcs *.c* *.S)
add_library(_PIODUMMY INTERFACE)
zephyr_library()
zephyr_library_sources(${module_srcs})
"""

    module_cfg_tpl = """# Warning! Do not delete this auto-generated file.
build:
  cmake: .
"""

    dummy_component_path = os.path.join(FRAMEWORK_DIR, "_pio", "_bare_module")
    if not os.path.isdir(dummy_component_path):
        os.makedirs(dummy_component_path)

    for ext in (".cpp", ".c", ".S"):
        dummy_src_file = os.path.join(dummy_component_path, "__dummy" + ext)
        if not os.path.isfile(dummy_src_file):
            open(dummy_src_file, "a").close()

    component_cmake = os.path.join(dummy_component_path, "CMakeLists.txt")
    if not os.path.isfile(component_cmake):
        with open(component_cmake, "w") as fp:
            fp.write(prj_cmake_tpl)

    zephyr_module_config = os.path.join(
        dummy_component_path, "zephyr", "module.yml"
    )
    if not os.path.isfile(zephyr_module_config):
        if not os.path.isdir(zephyr_module_config):
            os.makedirs(os.path.dirname(zephyr_module_config))
        with open(zephyr_module_config, "w") as fp:
            fp.write(module_cfg_tpl)

    return dummy_component_path


def get_default_build_flags(app_config, default_config):
    assert default_config

    def _extract_flags(config):
        flags = {}
        for cg in config.get("compileGroups", []):
            flags[cg["language"]] = []
            for ccfragment in cg["compileCommandFragments"]:
                fragment = ccfragment.get("fragment", "")
                if not fragment.strip() or fragment.startswith("-D"):
                    continue
                flags[cg["language"]].extend(
                    click.parser.split_arg_string(fragment.strip())
                )

        return flags

    app_flags = _extract_flags(app_config)
    default_flags = _extract_flags(default_config)

    return {
        "ASPPFLAGS": app_flags.get("ASM", default_flags.get("ASM")),
        "CFLAGS": app_flags.get("C", default_flags.get("C")),
        "CXXFLAGS": app_flags.get("CXX", default_flags.get("CXX")),
    }


def is_project_required(project_config):
    project_name = project_config["name"]
    if project_name.startswith("hal_") and project_name[
        4:
    ] not in PLATFORMS_WITH_EXTERNAL_HAL.get(env.subst("$PIOPLATFORM"), []):
        return False

    if project_config["path"].startswith("tool") or project_name.startswith(
        "nrf_hw_"
    ):
        return False

    return True


def get_default_module_config(target_configs):
    for config in target_configs:
        if "_pio___bare_module" in config:
            return target_configs[config]
    return {}


def get_standard_libs(project_settings, modules_map, preliminary_elf_path):
    # Starting from v4.0 Zephyr uses the `picolibc` library by default
    # instead of c library bundled with the toolchain.
    # See '/cmake/linker/ld/linker_libraries.cmake'
    # Currently, standard libraries are not exported in the CMake code model
    # as they require a precise position in the linker command

    result = []

    if project_settings.get("CONFIG_MINIMAL_LIBC", ""):
        result.append("-lgcc")

    elif project_settings.get("CONFIG_NEWLIB_LIBC", ""):
        result.extend(["-lm", "-lc", "-lgcc", "-lc"])

        if (
            project_settings.get("CONFIG_CPP", "")
            and not project_settings.get("CONFIG_MINIMAL_LIBCPP", "")
            and not project_settings.get("CONFIG_NATIVE_LIBRARY", "")
            and not project_settings.get("CONFIG_EXTERNAL_MODULE_LIBCPP", "")
        ):
            # The order of libs is important, libstdc++ goes first
            result.insert(0, "-lstdc++")

        return result

    elif project_settings.get("CONFIG_PICOLIBC", ""):
        # picolibc is the default choice
        picolib = (
            [v for k, v in modules_map.items() if k.startswith("c::")][0]
            if any(k.startswith("c::") for k in modules_map.keys())
            else None
        )

        assert (
            picolib
        ), "Error: picolibc is used but there is no picolibc configuration"

        # Note: picolibc is added manually at a specific position in linker command
        env.Depends(preliminary_elf_path, picolib["lib_path"])

        return [
            picolib["lib_path"].get_abspath(),
            "-lgcc",
        ]

    return result


def process_project_lib_deps(
    modules_map,
    project_libs,
    preliminary_elf_path,
    project_settings,
    offset_lib,
    lib_paths,
):
    # Get rid of the `app` library as the project source files are handled by
    # PlatformIO and linker as object files in the linker command
    # whole_libs = [lib for lib in project_libs["whole_libs"]]
    whole_libs = [
        lib
        for lib in project_libs["whole_libs"]
        if "app" not in lib or ZEPHYR_APP_BUILD_CONTROL
    ]

    # std libs extracted from project settings
    stdlibs = get_standard_libs(
        project_settings, modules_map, preliminary_elf_path
    )

    # Some of the project libraries should be linked entirely, so they are
    # manually wrapped inside the `--whole-archive` and `--no-whole-archive`
    # flags.
    env.Append(
        LIBPATH=lib_paths,
        _LIBFLAGS=" -Wl,--whole-archive "
        + " ".join(
            [os.path.join("$BUILD_DIR", library) for library in whole_libs]
            + [offsets_lib[0].get_abspath()]
        )
        + " -Wl,--no-whole-archive "
        + " ".join(
            [
                os.path.join("$BUILD_DIR", library)
                for library in project_libs["generic_libs"]
            ]
            # std libs from CMake model
            + project_libs["standard_libs"]
            # std libs extracted from project settings
            + stdlibs
        ),
    )

    # Note: These libraries are not added to the `LIBS` section. Hence they
    # must be specified as explicit dependencies.
    env.Depends(
        preliminary_elf_path,
        [
            os.path.join("$BUILD_DIR", library)
            for library in project_libs["generic_libs"] + whole_libs
            if "app" not in library or ZEPHYR_APP_BUILD_CONTROL
        ],
    )


def get_zephyr_venv_dir():
    # The name of the Zephyr venv contains the Zephyr version to avoid possible
    # conflicts and unnecessary reinstallation of Python dependencies in
    # PlatformIO venv
    zephyr_version = version.get_original_version(
        platform.get_package_version("framework-zephyr")
    )
    return os.path.join(
        env.subst("$PROJECT_CORE_DIR"), "penv", ".zephyr-" + zephyr_version
    )


def install_python_deps():
    def _get_installed_pip_packages(python_exe_path):
        result = {}
        packages = {}
        pip_output = subprocess.check_output(
            [
                python_exe_path,
                "-m",
                "pip",
                "list",
                "--format=json",
                "--disable-pip-version-check",
            ]
        )
        try:
            packages = json.loads(pip_output)
        except Exception:
            print(
                "Warning! Couldn't extract the list of installed Python packages."
            )
            return {}
        for p in packages:
            result[p["name"]] = version.pepver_to_semver(p["version"])

        return result

    deps = {
        # Zephyr
        "pyelftools": "~=0.27",
        "PyYAML": "~=6.0.0",
        "pykwalify": "~=1.8.0",
        "packaging": "~=23.1.0",
        # MCUboot
        "cryptography": ">=2.6.0",
        "intelhex": "~=2.3.0",
        "click": "~=8.1.3",
        "cbor2": "~=5.4.6",
    }

    python_exe_path = get_python_exe()
    installed_packages = _get_installed_pip_packages(python_exe_path)
    packages_to_install = []
    for package, spec in deps.items():
        if package not in installed_packages:
            packages_to_install.append(package)
        elif spec:
            version_spec = semantic_version.Spec(spec)
            if not version_spec.match(installed_packages[package]):
                packages_to_install.append(package)

    if IS_WINDOWS and "windows-curses" not in installed_packages:
        env.Execute(
            env.VerboseAction(
                '"%s" -m pip install windows-curses' % python_exe_path,
                "Installing windows-curses package",
            )
        )

    if packages_to_install:
        env.Execute(
            env.VerboseAction(
                (
                    '"%s" -m pip install -U ' % python_exe_path
                    + " ".join(
                        ['"%s%s"' % (p, deps[p]) for p in packages_to_install]
                    )
                ),
                "Installing Zephyr's Python dependencies",
            )
        )


def ensure_python_venv_available():
    def _is_venv_outdated(venv_data_file):
        try:
            with open(venv_data_file, "r", encoding="utf8") as fp:
                venv_data = json.load(fp)
                if venv_data.get("version", "") != ZEPHYR_ENV_VERSION:
                    return True
                return False
        except Exception:
            return True

    def _create_venv(venv_dir):
        pip_path = os.path.join(
            venv_dir,
            "Scripts" if IS_WINDOWS else "bin",
            "pip" + (".exe" if IS_WINDOWS else ""),
        )

        if os.path.isdir(venv_dir):
            try:
                print("Removing an oudated Zephyr virtual environment")
                shutil.rmtree(venv_dir)
            except OSError:
                print(
                    "Error: Cannot remove an outdated Zephyr virtual "
                    "environment. Please remove the `%s` folder manually!"
                    % venv_dir
                )
                env.Exit(1)

        # Use the built-in PlatformIO Python to create a standalone
        # Zephyr virtual env
        env.Execute(
            env.VerboseAction(
                '"$PYTHONEXE" -m venv --clear "%s"' % venv_dir,
                "Creating a new virtual environment for Zephyr "
                "Python dependencies",
            )
        )

        assert os.path.isfile(pip_path), (
            "Error: Failed to create a proper virtual environment. "
            "Missing the `pip` binary!"
        )

    venv_dir = get_zephyr_venv_dir()
    venv_data_file = os.path.join(venv_dir, "pio-zephyr-venv.json")
    if not os.path.isfile(venv_data_file) or _is_venv_outdated(venv_data_file):
        _create_venv(venv_dir)
        with open(venv_data_file, "w", encoding="utf8") as fp:
            venv_info = {"version": ZEPHYR_ENV_VERSION}
            json.dump(venv_info, fp, indent=2)


def get_python_exe():
    python_exe_path = os.path.join(
        get_zephyr_venv_dir(),
        "Scripts" if IS_WINDOWS else "bin",
        "python" + (".exe" if IS_WINDOWS else ""),
    )

    assert os.path.isfile(python_exe_path), (
        "Error: Missing Python executable file `%s`" % python_exe_path
    )

    return python_exe_path


def install_bundled_projects(manifest_path):
    cmd = (
        get_python_exe(),
        os.path.join(FRAMEWORK_DIR, "scripts", "platformio", "install-deps.py"),
        "--platform",
        platform.name,
        "--secondary-installation",
        "--manifest",
        manifest_path,
    )

    rc = subprocess.call(cmd)
    if rc != 0:
        sys.stderr.write("Error: Couldn't install Zephyr dependencies.\n")
        env.Exit(1)


def get_libcc_dir():
    cmd = [env.subst("$CC"), "--print-libgcc-file-name"] + [
        f for f in env.get("CFLAGS", []) if f.startswith("-m")
    ]
    result = exec_command(cmd)
    if result["returncode"] != 0:
        sys.stderr.write(result["out"] + "\n")
        sys.stderr.write(result["err"])
        env.Exit(1)

    libgcc_dir = os.path.dirname(result["out"].strip())
    assert os.path.isdir(libgcc_dir), "Failed to extract libgcc directory!"

    return libgcc_dir


def GenerateMCUbootBinaryCmd(env, target, source):
    if "mcuboot-image" not in COMMAND_LINE_TARGETS:
        return None

    signature_key = get_boot_signature_key_file(project_settings)
    boot_header_len = board.get("build.zephyr.bootloader.header_len", "")
    flash_alignment = board.get("build.zephyr.bootloader.flash_alignment", "")
    slot_size = board.get("build.zephyr.bootloader.slot_size", "")

    if not boot_header_len:
        sys.stderr.write(
            "Error: Cannot generate an image for MCUboot. The "
            "`board_build.zephyr.bootloader.header_len` option is not set!\n"
        )
        env.Exit(1)

    if not flash_alignment:
        sys.stderr.write(
            "Error: Cannot generate an image for MCUboot. The "
            "`board_build.zephyr.bootloader.flash_alignment` option "
            "is not set!\n"
        )
        env.Exit(1)

    if not slot_size:
        sys.stderr.write(
            "Error: Cannot generate an image for MCUboot. The "
            "`board_build.zephyr.bootloader.slot_size` option is not set!\n"
        )
        env.Exit(1)

    cmd = [
        get_python_exe(),
        '"%s"'
        % os.path.join(
            FRAMEWORK_DIR,
            "_pio",
            "bootloader",
            "mcuboot",
            "scripts",
            "imgtool.py",
        ),
        "sign",
    ]

    if signature_key:
        cmd.extend(["--key", signature_key])
    else:
        print("Warning! The signature key is not specified!")

    if board.get("build.zephyr.bootloader.secondary_slot", ""):
        cmd.append("--pad")

    if board.get("build.zephyr.bootloader.imgtool_extra_cmds", ""):
        cmd.extend(
            click.parser.split_arg_string(
                board.get("build.zephyr.bootloader.imgtool_extra_cmds")
            )
        )

    cmd.extend(
        [
            "--header-size",
            boot_header_len,
            "--align",
            flash_alignment,
            "--version",
            board.get("build.zephyr.bootloader.app_version", "0.0.0"),
            "--slot-size",
            slot_size,
            "$SOURCE",
            "$TARGET",
        ]
    )

    return env.Command(
        target,
        source,
        env.VerboseAction(" ".join(cmd), "Signing $TARGET"),
    )


#
# Current build script limitations
#

if " " in FRAMEWORK_DIR:
    sys.stderr.write(
        "Error: Detected a whitespace character in framework path\n"
    )
    env.Exit(1)

#
# Install Python dependencies
#

ensure_python_venv_available()
install_python_deps()

#
# Install Zephyr dependencies
#

west_manifest, manifest_path = load_west_manifest()
install_bundled_projects(manifest_path)

#
# Initial targets loading
#

codemodel = get_cmake_code_model(west_manifest)
if not codemodel:
    sys.stderr.write("Error: Couldn't find code model generated by CMake\n")
    env.Exit(1)

target_configs = load_target_configurations(codemodel)

app_config = target_configs.get("app")
stage0_config = target_configs.get("zephyr_pre0")

if not app_config or not stage0_config:
    sys.stderr.write(
        "Error: Couldn't find main Zephyr target in the code model\n"
    )
    if int(ARGUMENTS.get("PIOVERBOSE", 0)):
        print("Available targets: ", *target_configs.keys(), sep=" ")
    env.Exit(1)

project_settings = load_project_settings()
cmake_settings = load_cmake_settings()

#
# Generate prerequisite files
#

relocation_files = None
if project_settings.get("CONFIG_CODE_DATA_RELOCATION", ""):
    if not ZEPHYR_PRESERVE_OBJ_EXT:
        print(
            "Warning: Object file extension has been automatically switched "
            "to `.obj` to properly generate relocation files!"
        )
        ZEPHYR_PRESERVE_OBJ_EXT = True
    relocation_files = generate_relocation_files_cmd()

offset_header_file = generate_offset_header_file_cmd()
syscalls_config = parse_syscalls()
generate_syscall_files(syscalls_config, project_settings)
generate_kobject_files()
validate_driver()
generate_version_header()

if project_settings.get("CONFIG_MINIMAL_LIBC", ""):
    generate_strerror_table(project_settings)

if project_settings.get("CONFIG_BOOT_SIGNATURE_KEY_FILE", ""):
    generate_pubkey(get_boot_signature_key_file(project_settings))

if project_settings.get("CONFIG_POSIX_SIGNALS", ""):
    generate_strsignal_table()


#
# LD scripts processing
#

app_includes = get_app_includes(app_config)
stage0_ldscript = get_linkerscript_cmd(
    app_includes,
    "linker_zephyr_pre0.cmd",
    project_settings,
    cmake_settings,
    extra_flags=["-DLINKER_DEVICE_DEPS_PASS1"],
)

final_ld_script = get_linkerscript_cmd(
    app_includes,
    "linker.cmd",
    project_settings,
    cmake_settings,
    extra_flags=["-DLINKER_ZEPHYR_FINAL"],
)

for ldscript in (stage0_ldscript, final_ld_script):
    env.Depends(ldscript, relocation_files)
    env.Depends(ldscript, offset_header_file)

env.Depends(final_ld_script, stage0_ldscript)


#
# Includible files processing
#

if (
    "generate_inc_file_for_target"
    in app_config.get("backtraceGraph", {}).get("commands", [])
    and "build.embed_files" not in board
):
    print(
        "Warning! Detected a custom CMake command for embedding files. "
        "Please use 'board_build.embed_files' option in 'platformio.ini' "
        "to include files!"
    )

if "build.embed_files" in board:
    for f in board.get("build.embed_files", "").split():
        file = os.path.join(PROJECT_DIR, f)
        if not os.path.isfile(env.subst(f)):
            print('Warning! Could not find file "%s"' % os.path.basename(f))
            continue

        env.Depends(offset_header_file, generate_includible_file(file))

#
# Libraries processing
#

IGNORED_LIBS = (
    # Ignore app library if user didn't disable it deliberately
    "app" if not ZEPHYR_APP_BUILD_CONTROL else "",
    "offsets",
)

framework_modules_map = {}
for target, target_config in target_configs.items():
    lib_name = target_config["name"]
    if (
        target_config["type"]
        not in (
            "STATIC_LIBRARY",
            "OBJECT_LIBRARY",
        )
        or lib_name in IGNORED_LIBS
    ):
        continue

    lib = build_library(env, target_config, PROJECT_SRC_DIR)
    framework_modules_map[target_config["id"]] = {
        "lib_path": lib[0],
        "config": target_config,
    }

    if any(
        d.get("id", "").startswith("zephyr_generated_headers")
        for d in target_config.get("dependencies", [])
    ):
        env.Depends(lib[0].sources, offset_header_file)

# Offsets library compiled separately as it's used later for custom dependencies
offsets_lib = build_library(env, target_configs["offsets"], PROJECT_SRC_DIR)

# A special case for the autogenerated relocation file that can depend on
# arbitrary source files passed implicitly
if project_settings.get("CONFIG_CODE_DATA_RELOCATION", ""):
    code_relocation_lib_config = target_configs.get(
        "code_relocation_source_lib", {}
    )
    if code_relocation_lib_config:
        reloc_lib_deps = get_project_lib_deps(
            framework_modules_map, code_relocation_lib_config
        )
        for lib_dep in reloc_lib_deps.values():
            env.Depends(
                os.path.join("$BUILD_DIR", "zephyr", "code_relocation.c"),
                lib_dep["lib_path"],
            )
    else:
        print(
            "Warning! The code relocation option is enabled in project configuration, "
            "but the relocation library is not available!"
        )

#
# Preliminary ELFs and subsequent targets
#

stage0_elf_path = os.path.join("$BUILD_DIR", "zephyr", "firmware-pre0.elf")
for dep in (offsets_lib, stage0_ldscript):
    env.Depends(stage0_elf_path, dep)

if project_settings.get("CONFIG_DEVICE_DEPS", ""):
    dev_handles = generate_dev_handles(stage0_elf_path, project_settings)

isr_table_file = generate_isr_table_file_cmd(
    stage0_elf_path, board, project_settings
)

#
# Final firmware targets
#

env.Append(
    PIOBUILDFILES=compile_source_files(stage0_config, env, PROJECT_SRC_DIR),
    _EXTRA_ZEPHYR_PIOBUILDFILES_FINAL=compile_source_files(
        target_configs["zephyr_final"], env, PROJECT_SRC_DIR
    ),
    __ZEPHYR_OFFSET_HEADER_CMD=offset_header_file,
)

for dep in (isr_table_file, final_ld_script):
    env.Depends("$PROG_PATH", dep)

linker_arguments = extract_link_args(target_configs["zephyr_final"])

# remove the main linker script flags '-T linker.cmd'
try:
    ld_index = linker_arguments["link_flags"].index("linker.cmd")
    linker_arguments["link_flags"].pop(ld_index)
    linker_arguments["link_flags"].pop(ld_index - 1)
except Exception:
    pass

# Flags shouldn't be merged automatically as they have precise position
# in the linker cmd
ignore_flags = (
    "CMakeFiles",
    "-Wl,--whole-archive",
    "-Wl,--no-whole-archive",
    "-Wl,-T",
    "-T",
)
linker_arguments["link_flags"] = filter_args(
    linker_arguments["link_flags"], ["-"], ignore_flags
)

#
# On this stage project libraries are placed in proper places inside the linker
# command
#

process_project_lib_deps(
    framework_modules_map,
    linker_arguments["project_libs"],
    stage0_elf_path,
    project_settings,
    offsets_lib,
    linker_arguments["lib_paths"],
)

#
# Here default build flags pulled from the `app` configuration
#

env.Replace(ARFLAGS=["qc"])
env.Append(
    CPPPATH=app_includes["plain_includes"],
    CCFLAGS=[("-isystem", inc) for inc in app_includes.get("sys_includes", [])],
    CPPDEFINES=get_app_defines(app_config),
    LINKFLAGS=linker_arguments["link_flags"],
)

build_flags = get_default_build_flags(
    app_config, get_default_module_config(target_configs)
)
env.Append(**build_flags)

#
# Add C++ runtime object files in a specific position of the linker command
# if C++ exceptions are enabled:
# See: cmake/linker/ld/target.cmake
#

if project_settings.get("CONFIG_CPP_EXCEPTIONS", ""):
    libgcc_dir = get_libcc_dir()
    env.Replace(
        LINKCOM=env["LINKCOM"].replace(
            "$_LIBFLAGS",
            " ".join(
                [
                    os.path.join(libgcc_dir, "crtbegin.o"),
                    "$_LIBFLAGS",
                    os.path.join(libgcc_dir, "crtend.o"),
                ]
            ),
        )
    )

#
# Custom builders required
#

env.Append(
    BUILDERS=dict(
        ElfToBin=Builder(
            action=env.VerboseAction(
                " ".join(
                    [
                        "$OBJCOPY",
                        "--gap-fill",
                        "0xff",
                        "--remove-section=.comment",
                        "--remove-section=COMMON",
                        "--remove-section=.eh_frame",
                        "-O",
                        "binary",
                        "$SOURCES",
                        "$TARGET",
                    ]
                ),
                "Building $TARGET",
            ),
            suffix=".bin",
        ),
        ElfToHex=Builder(
            action=env.VerboseAction(
                " ".join(
                    [
                        "$OBJCOPY",
                        "-O",
                        "ihex",
                        "--remove-section=.comment",
                        "--remove-section=COMMON",
                        "--remove-section=.eh_frame",
                        "$SOURCES",
                        "$TARGET",
                    ]
                ),
                "Building $TARGET",
            ),
            suffix=".hex",
        ),
    )
)

if get_board_architecture(board) == "arm":
    env.Replace(
        SIZEPROGREGEXP=r"^(?:text|_TEXT_SECTION_NAME_2|sw_isr_table|devconfig|rodata|\.ARM.exidx)\s+(\d+).*",
        SIZEDATAREGEXP=r"^(?:datas|bss|noinit|initlevel|_k_mutex_area|_k_stack_area)\s+(\d+).*",
    )

#
# Target: menuconfig
#

env.AddPlatformTarget(
    "menuconfig",
    None,
    [env.VerboseAction(RunMenuconfig, "Running menuconfig")],
    "Run Menuconfig",
)

#
# MCUboot image target
#

env.AddPlatformTarget(
    name="mcuboot-image",
    dependencies=["$BUILD_DIR/${PROGNAME}.mcuboot.bin"],
    actions=None,
    title="Generate MCUboot Image",
    description="Generate firmware binary to be loaded by MCUboot",
)

env.AddMethod(GenerateMCUbootBinaryCmd, "MCUbootImage")
