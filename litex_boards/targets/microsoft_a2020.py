#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2026 blurbdust <blurbdust@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

# Bring-up SoC for the Microsoft "A-2020" datacentre accelerator card (Stratix 10 GX 2800).
#
# Stratix 10 is Quartus Prime PRO only. main() overrides LiteX's Standard-flow tool names with
# --synth-tool quartus_syn / --conv-tool quartus_pfg, so no extra flags are needed on the command
# line; pass them explicitly only to force the Standard flow, which cannot build this device.
#
# There is no hardware UART on this board. Storey Peak falls back to jtag_uart, but that is not
# available here: LiteX's AlteraJTAG needs a <family>_jtag atom exposing the user TAP states
# (shiftuser/updateuser/clkdruser/usr1user), and Stratix 10 has none. Its JTAG goes through the SDM,
# whose atoms (fourteennm_sdm_jtag_ela) do not carry those outputs, which is what the TODO at
# litex/soc/cores/jtag.py:245 refers to. So the console defaults to crossover, reachable once a bus
# bridge exists; --with-jtagbone will raise NotImplementedError until that TODO is done.
#
# --with-pcie brings up the PCIe endpoint, which is the only way into this SoC. The DDR4 and QSFP28
# pins are declared in the platform file but are not driven here.

import os
import re
import shutil
import hashlib
import subprocess

from migen import *

from litex_boards.platforms import microsoft_a2020

from litex.gen import *

from litex.build.generic_platform import Pins, Subsignal

from litex.soc.cores.clock.intel_stratix10 import Stratix10PLL
from litex.soc.integration.soc import *
from litex.soc.integration.builder import *
from litex.soc.cores.led import LedChaser
from litex.soc.cores.bitbang import I2CMaster

# Reset Release IP ---------------------------------------------------------------------------------

# Stratix 10 hands the fabric over before device initialisation has finished, so user logic that
# runs in that window behaves unpredictably. Intel's Reset Release IP drives ninit_done, which stays
# asserted until initialisation completes. Quartus raises Critical Warning (20615) on any Stratix 10
# design that does not carry it.
#
# The IP has no pre-generated RTL to point at: altera_s10_user_rst_clkgate.sv is expanded from a
# .terp template by the component's _hw.tcl, so it has to go through Platform Designer. The copy in
# <quartus>/ip/altera/oran/example_design/common/modules/ is a simulation stub built out of initial
# blocks and does not synthesise.
#
# Generated from Tcl at build time and cached on a hash of that Tcl, following
# litepcie.phy.svpciephy, which generates the Stratix V transceiver reconfiguration controller the
# same way.

def _find_qsys_tool(name):
    """Locate a Qsys binary.

    The Qsys tools live in <quartus>/sopc_builder/bin, which is deliberately *not* on PATH in a
    normal Quartus setup (only <quartus>/bin is), so searching PATH alone is not enough.
    """
    found = shutil.which(name)
    if found is not None:
        return found

    anchor = shutil.which("quartus_sh") or shutil.which("quartus_syn")
    if anchor is not None:
        qbin = os.path.dirname(os.path.realpath(anchor))
        cand = os.path.normpath(os.path.join(qbin, "..", "sopc_builder", "bin", name))
        if os.path.exists(cand):
            return cand

    raise OSError(
        f"Could not find {name}. The Qsys tools ship in <quartus>/sopc_builder/bin, which is "
        f"usually not on PATH; add that directory to PATH."
    )

def _build_reset_release_qsys_tcl(name, instance, device, family):
    # DEVICE_FAMILY is a SYSTEM_INFO parameter of the component, so it follows the project property
    # and must not be set on the instance. outputType stays on the conduit form used by the prior
    # art; the reset form would add a Platform Designer reset interface this system has nothing to
    # connect to.
    return "\n".join([
        "package require -exact qsys 16.0",
        f"create_system {name}",
        f"set_project_property DEVICE_FAMILY {{{family}}}",
        f"set_project_property DEVICE {{{device}}}",
        f"add_instance {instance} altera_s10_user_rst_clkgate",
        f"set_instance_parameter_value {instance} outputType {{Conduit Interface}}",
        "add_interface ninit_done conduit end",
        f"set_interface_property ninit_done EXPORT_OF {instance}.ninit_done",
        f"save_system {name}.qsys",
    ]) + "\n"

def _collect_qips(output_dir):
    """Every .qip the generation wrote, so the whole hierarchy reaches Quartus.

    Platform Designer splits a system into one .qip for the system wrapper and one per instance
    under ip/<system>/, and the system .qip does not reference the instance ones. Registering only
    the system .qip leaves altera_s10_user_rst_clkgate with no source and synthesis infers a black
    box.
    """
    qips = []
    for root, _, files in os.walk(output_dir):
        qips += [os.path.join(root, f) for f in files if f.endswith(".qip")]
    if not qips:
        raise OSError(f"[ResetRelease] no .qip found under {output_dir}.")
    return sorted(qips)

def generate_reset_release(name, instance, device, family, output_dir, force=False):
    """Generate the Reset Release IP as a Qsys system and return (qip paths, top Verilog path).

    Cached on a hash of everything that affects the output, like litepcie.phy.svpciephy.
    """
    tcl = _build_reset_release_qsys_tcl(name, instance, device, family)
    _CACHE_VERSION = "1"
    hash = hashlib.sha256((_CACHE_VERSION + tcl).encode()).hexdigest()[:16]

    output_dir = os.path.abspath(output_dir)
    stampfile  = os.path.join(output_dir, f"{name}.litex_hash")
    top        = os.path.join(output_dir, name, "synth", f"{name}.v")

    if not force and os.path.exists(top) and os.path.exists(stampfile):
        if open(stampfile).read().strip() == hash:
            print(f"[ResetRelease] Reusing cached Reset Release IP in {output_dir} (hash {hash}).")
            return _collect_qips(output_dir), top

    # Drop what a previous generation left. The .qip set is collected by walking output_dir, so a
    # stale one from different parameters would otherwise be handed to Quartus alongside the new
    # ones. Only the paths Platform Designer creates are removed, never output_dir itself.
    for stale in [name, "ip", "qdb", "dni"]:
        shutil.rmtree(os.path.join(output_dir, stale), ignore_errors=True)

    qsys_script   = _find_qsys_tool("qsys-script")
    qsys_generate = _find_qsys_tool("qsys-generate")

    os.makedirs(output_dir, exist_ok=True)
    tclfile = os.path.join(output_dir, f"{name}.tcl")
    with open(tclfile, "w") as f:
        f.write(tcl)

    print(f"[ResetRelease] Generating {name} for {device}.")
    for cmd in [
        [qsys_script,   f"--script={name}.tcl"],
        [qsys_generate, f"{name}.qsys", "--synthesis=VERILOG", f"--family={family}",
                        f"--part={device}"],
    ]:
        r = subprocess.run(cmd, cwd=output_dir)
        if r.returncode != 0:
            raise OSError(
                f"[ResetRelease] {os.path.basename(cmd[0])} failed (exit {r.returncode}).\n"
                f"  cmd: {' '.join(cmd)}\n  cwd: {output_dir}\n"
                f"Rerun it by hand there to see the full Qsys log."
            )
    if not os.path.exists(top):
        raise OSError(f"[ResetRelease] generation reported success but {top} is missing.")

    with open(stampfile, "w") as f:
        f.write(hash)
    return _collect_qips(output_dir), top

def probe_ninit_done_port(top_verilog):
    """Read the port name off the generated top level.

    Read rather than assumed: Platform Designer names an exported conduit port
    <interface>_<port>, giving ninit_done_ninit_done here, but that is a naming convention and not
    a contract, and a silently wrong port name would leave ninit_done tied low.
    """
    src = open(top_verilog).read()
    m   = re.search(r"output\s+wire\s+(\w*ninit_done\w*)", src)
    if m is None:
        raise ValueError(f"Could not find a ninit_done output in {top_verilog}.")
    return m.group(1)

class ResetRelease(LiteXModule):
    """Intel Reset Release IP (altera_s10_user_rst_clkgate), generated at build time."""
    def __init__(self, platform, name="reset_release", instance="rr", family="Stratix 10",
        output_dir=None):
        self.ninit_done = Signal()

        # # #

        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), "build", f"{platform.name}_ip")

        qips, top = generate_reset_release(
            name       = name,
            instance   = instance,
            device     = platform.device,
            family     = family,
            output_dir = output_dir,
        )
        for qip in qips:
            platform.add_ip(qip)

        self.specials += Instance(name, **{f"o_{probe_ninit_done_port(top)}": self.ninit_done})

# PCIe lane subset ---------------------------------------------------------------------------------

def request_pcie_lanes(platform, nlanes, name="pcie_x16", number=0):
    """Request the first `nlanes` lanes of the x16 edge connector.

    Requesting pcie_x16 and driving only the first few transceivers is not an option: every pin of a
    requested resource gets a location assignment, and a transceiver pin constrained but left
    without a driver is a fitter error. So a narrower resource is derived here and requested
    instead, leaving pcie_x16 unrequested and therefore unconstrained.

    The pin names are read back out of the platform's own resource; none is retyped, so this cannot
    drift from the platform file or invent a pin.
    """
    resource = None
    for r in platform.constraint_manager.available:
        if r[0] == name and r[1] == number:
            resource = r
            break
    if resource is None:
        raise ValueError(f"{name}:{number} is not available on this platform (already requested?).")

    lanes = [len(c.identifiers) for s in resource[2:] if isinstance(s, Subsignal)
             and s.name in ["tx_p", "tx_n", "rx_p", "rx_n"]
             for c in s.constraints if isinstance(c, Pins)]
    if not lanes:
        raise ValueError(f"{name}:{number} declares no tx_p/rx_p pins.")
    if nlanes > min(lanes):
        raise ValueError(f"{name}:{number} carries {min(lanes)} lanes, {nlanes} requested.")
    if nlanes == min(lanes) == max(lanes):
        return platform.request(name, number)

    subsignals = []
    for item in resource[2:]:
        if not isinstance(item, Subsignal) or item.name not in ["tx_p", "tx_n", "rx_p", "rx_n"]:
            subsignals.append(item)
            continue
        constraints = [Pins(" ".join(c.identifiers[:nlanes])) if isinstance(c, Pins) else c
                       for c in item.constraints]
        subsignals.append(Subsignal(item.name, *constraints))

    subset = f"pcie_x{nlanes}"
    platform.add_extension([(subset, number) + tuple(subsignals)])
    return platform.request(subset, number)

# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.rst    = Signal()
        self.cd_sys = ClockDomain()

        # # #

        # Clk / Rst.
        # No reset input is exposed on this board, so sys reset comes from the PLL lock only.
        clk100 = platform.request("clk100")

        # Reset Release.
        # ninit_done stays asserted until device initialisation finishes. Without it the sys domain
        # would start running during the window where Stratix 10 has already released the fabric but
        # the device is not initialised, and power-on behaviour is undefined.
        self.reset_release = ResetRelease(platform)

        # PLL.
        # speedgrade matches the E2 suffix of the 1SG280LN2F43E2VG device string.
        # ninit_done gates the PLL rather than cd_sys directly: create_clkout drives cd_sys reset
        # from ~locked, so holding the PLL in reset holds every domain it feeds.
        self.pll = pll = Stratix10PLL(platform, speedgrade="-2")
        self.comb += pll.reset.eq(self.rst | self.reset_release.ninit_done)
        pll.register_clkin(clk100, 100e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)

        # sys and clk100 are related only through the PLL. The SoC reset (sys domain) reaches
        # registers clocked by clk100 as an asynchronous reset, which STA otherwise times as a
        # synchronous CDC and reports as a setup violation. Constrain against the pad, not
        # pll.clkin: only the pad has a clock name for get_clocks to match.
        platform.add_false_path_constraints(self.cd_sys.clk, clk100)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCMini):
    def __init__(self, sys_clk_freq=100e6,
        with_led_chaser = True,
        with_i2c        = True,
        with_pcie       = False,
        pcie_speed      = "gen2",
        pcie_lanes      = 8,
        pcie_ndmas      = 0,
        **kwargs):
        self.platform = platform = microsoft_a2020.Platform()

        # quartus_pfg refuses .svf for this device: "Error (20718): Serial Vector Format file (.svf)
        # generation for the target device 1SG280LN2F43 is not supported." Without this the build
        # script's last step fails and --build returns non-zero even though the .sof and .rbf are
        # good. The other Quartus Pro boards set this in their platform file; it belongs there.
        platform.create_svf = False

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq)

        # SoCMini ----------------------------------------------------------------------------------
        # No hardware UART and no usable JTAG TAP on this family, so "serial" becomes crossover.
        if kwargs.get("uart_name", "serial") == "serial":
            kwargs["uart_name"] = "crossover"
        SoCMini.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on Microsoft A-2020", **kwargs)

        # Leds -------------------------------------------------------------------------------------
        if with_led_chaser:
            self.leds = LedChaser(
                pads         = platform.request_all("user_led"),
                sys_clk_freq = sys_clk_freq)

        # I2C --------------------------------------------------------------------------------------
        # Board-shared bus: DIMM SPD at 0x50, the DS250DF810 retimers and the FPC202.
        if with_i2c:
            self.i2c0 = I2CMaster(platform.request("i2c", 0))

        # PCIe -------------------------------------------------------------------------------------
        # Stratix 10 L-Tile PCIe hard IP, wrapped by litepcie.phy.s10pciephy. UNTESTED ON SILICON:
        # Gen2 x8 compiles and closes timing, but no link has ever been trained with it. This is the
        # board's only host interface, since Stratix 10 has no JTAG TAP that LiteX's AlteraJTAG can
        # drive (see the header comment).
        #
        # The endpoint is x8 rather than x16 by default because the prior art that proves this hard
        # IP on this board measured a Gen2 x8 link; an x8 endpoint in the x16 slot negotiates down.
        if with_pcie:
            # Imported here rather than at module scope: litepcie.phy.s10pciephy is not upstream yet.
            from litepcie.phy.s10pciephy import S10PCIEPHY
            self.pcie_phy = S10PCIEPHY(platform, request_pcie_lanes(platform, pcie_lanes),
                # The hard IP must be held off until device initialisation completes, same as the
                # rest of the fabric; the CRG already carries Intel's Reset Release IP for that.
                ninit_done = self.crg.reset_release.ninit_done,
                speed      = pcie_speed,
                nlanes     = pcie_lanes,
                # BAR0 has to cover the CSR region that add_pcie() maps into it.
                bar0_size  = 0x20000,
            )
            self.add_pcie(phy=self.pcie_phy, ndmas=pcie_ndmas)

# Build --------------------------------------------------------------------------------------------

def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=microsoft_a2020.Platform, description="LiteX SoC on Microsoft A-2020.")
    parser.add_target_argument("--sys-clk-freq",    default=100e6, type=float,        help="System clock frequency.")
    parser.add_target_argument("--with-led-chaser", action="store_true", default=True, help="Enable LED Chaser.")
    parser.add_target_argument("--with-i2c",        action="store_true", default=True, help="Enable I2C master.")
    parser.add_target_argument("--with-pcie",       action="store_true",              help="Enable PCIe endpoint via the Stratix 10 PCIe hard IP (UNTESTED ON SILICON).")
    parser.add_target_argument("--pcie-speed",      default="gen2", choices=["gen1", "gen2", "gen3"], help="PCIe link rate.")
    parser.add_target_argument("--pcie-lanes",      default=8, type=int, choices=[1, 2, 4, 8, 16], help="PCIe lane count (the edge is x16; a narrower endpoint negotiates down in an x16 slot).")
    parser.add_target_argument("--pcie-ndmas",      default=0, type=int,              help="Number of PCIe DMA channels. 0 gives BAR0 MMIO only.")
    parser.add_target_argument("--driver",          action="store_true",              help="Generate the LitePCIe kernel driver/userspace software.")

    # Stratix 10 is Quartus Prime PRO only; the Standard-flow tool names cannot build it.
    parser.set_defaults(synth_tool="quartus_syn")
    parser.set_defaults(conv_tool="quartus_pfg")
    # Bring-up SoC: no CPU unless one is asked for.
    parser.set_defaults(cpu_type="None")

    args = parser.parse_args()

    soc = BaseSoC(
        sys_clk_freq    = args.sys_clk_freq,
        with_led_chaser = args.with_led_chaser,
        with_i2c        = args.with_i2c,
        with_pcie       = args.with_pcie,
        pcie_speed      = args.pcie_speed,
        pcie_lanes      = args.pcie_lanes,
        pcie_ndmas      = args.pcie_ndmas,
        **parser.soc_argdict
    )
    builder = Builder(soc, **parser.builder_argdict)
    if args.driver:
        from litepcie.software import generate_litepcie_software
        generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))
    if args.build:
        builder.build(**parser.toolchain_argdict)

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram").replace(".sof", ".rbf"))

if __name__ == "__main__":
    main()
