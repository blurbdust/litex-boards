#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2026 blurbdust <blurbdust@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

# QSFP+ transceiver test SoC for the Microsoft Storey Peak (P/N X930613-001).
#
# One or both cages' channels run in PMA direct mode (liteiclink.serdes.sv_native) at --data-rate from
# the 644.53125 MHz QSFP reference clock, each with a PRBS7/15/31 generator and checker. Internal
# serial loopback (a CSR) proves the reference clock, the transmit PLLs, the CDRs and the PMA without
# a cable; a cable or loopback module then proves the lane pins. The I2C buses to the clock
# generator and the two cages are open-drain GPIO CSRs for a host-side bit-banged master.
#
# The CSRs are reached over the card's own JTAG through the Virtual JTAG link of the hashcat FPGA
# bridge (hcfpga_jtaglink.py in src/fpga/bcrypt of the hashcat tree, which must be on PYTHONPATH).
# LiteX's JTAGBone cannot be used: it needs the raw JTAG atom, which the reconfiguration
# controller's Nios debug node already claims through Intel's JTAG hub.
#
# Channels are numbered from cage 0 lane 0 (GXB_R10) up. Both cages use all six channels of bank R2,
# where a CMU PLL would cost a receiver, so they take ATX PLLs.
#
# LEDs: 0 heartbeat, 1 all TX PLLs locked, 2 all TX ready, 3 all RX ready, 4-7 PRBS locked on
# channels 0-3 (one cage) or on channel pairs 0-1, 2-3, 4-5, 6-7 (both cages).

import os
import datetime

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex_boards.platforms import microsoft_storey_peak

from litex.soc.cores.clock.intel_stratix5 import StratixVPLL
from litex.soc.interconnect.csr import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.builder import *

from liteiclink.serdes.sv_native import SVNative

from hcfpga_jtaglink import JTAGLink

QSFP_REFCLK_FREQ = 644.53125e6

# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, speedgrade):
        self.rst    = Signal()
        self.cd_sys = ClockDomain()

        # # #

        # 125 MHz free-running at power-on. No reset input on this board: the PLL lock is the reset.
        clk125 = platform.request("clk125")

        self.pll = pll = StratixVPLL(speedgrade=speedgrade)
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(clk125, 125e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)

        platform.toolchain.additional_sdc_commands += [
            "set_false_path -from [get_clocks sys_clk] -to [get_clocks clk125]",
            "set_false_path -from [get_clocks clk125] -to [get_clocks sys_clk]",
        ]

# I2C Pins -----------------------------------------------------------------------------------------

class I2CPins(LiteXModule):
    """Both lines of an I2C bus as open-drain GPIOs. The host bit-bangs the master and can use
    either line as SCL, which a bus whose SCL and SDA assignment is in doubt needs."""
    def __init__(self, pads):
        self._w = CSRStorage(description="Pull a line low.", fields=[
            CSRField("scl_low", size=1, offset=0),
            CSRField("sda_low", size=1, offset=1),
        ])
        self._r = CSRStatus(description="Line levels.", fields=[
            CSRField("scl", size=1, offset=0),
            CSRField("sda", size=1, offset=1),
        ])

        # # #

        for line in ["scl", "sda"]:
            t = TSTriple()
            self.specials += t.get_tristate(getattr(pads, line))
            self.comb += [
                t.o.eq(0),
                t.oe.eq(getattr(self._w.fields, line + "_low")),
            ]
            self.specials += MultiReg(t.i, getattr(self._r.fields, line))

# SoC ----------------------------------------------------------------------------------------------

class _Pads:
    def __init__(self, tx_p, rx_p):
        self.tx_p = tx_p
        self.rx_p = rx_p

class QSFPTestSoC(SoCMini):
    def __init__(self, device, sys_clk_freq=100e6, cages=[0], data_rate=10.3125e9, data_width=80,
        pll_type=None, ip_dir="ip", threads=None):
        platform = microsoft_storey_peak.Platform(device=device)
        speedgrade = "-" + {"5SGSMD5K1F40C1": "C1", "5SGSMD5K2F40I3L": "I3L"}[device]

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq, speedgrade)

        # SoCMini ----------------------------------------------------------------------------------
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        SoCMini.__init__(self, platform, sys_clk_freq,
            ident         = f"storeypeak-qsfp-test;cages={''.join(str(c) for c in cages)};rate={data_rate/1e6:g};width={data_width};device={device};build={stamp}",
            ident_version = False,
        )

        if threads is not None:
            platform.toolchain.additional_qsf_commands += [
                f"set_global_assignment -name NUM_PARALLEL_PROCESSORS {threads}"]

        # JTAG Link --------------------------------------------------------------------------------
        self.jtaglink = JTAGLink(platform, csr_base=self.mem_map.get("csr", 0))
        self.jtaglink.add_timing_constraints(platform)
        self.bus.add_master(name="jtaglink", master=self.jtaglink.bus)

        # QSFP Transceivers ------------------------------------------------------------------------
        refclk = platform.request("qsfp_refclk")
        cage_pads = [platform.request("qsfp", c) for c in cages]
        if pll_type is None:
            pll_type = "ATX" if len(cages) > 1 else "CMU"
        self.qsfp = SVNative(platform,
            pads         = _Pads(Cat(*[p.tx_p for p in cage_pads]), Cat(*[p.rx_p for p in cage_pads])),
            refclk       = refclk,
            refclk_freq  = QSFP_REFCLK_FREQ,
            data_rate    = data_rate,
            sys_clk_freq = sys_clk_freq,
            data_width   = data_width,
            name         = "qsfp" + "".join(str(c) for c in cages),
            output_dir   = ip_dir,
            pll_type     = pll_type,
        )
        self.qsfp.add_controls()
        self.qsfp.add_timing_constraints(platform, refclk, QSFP_REFCLK_FREQ)

        # I2C: clock generator, cage 0, cage 1 -----------------------------------------------------
        self.i2c_idt   = I2CPins(platform.request("i2c", 0))
        self.i2c_qsfp0 = I2CPins(platform.request("i2c", 1))
        self.i2c_qsfp1 = I2CPins(platform.request("i2c", 2))

        # LEDs -------------------------------------------------------------------------------------
        leds  = platform.request_all("user_led")
        beat  = Signal(27)
        n     = self.qsfp.nchannels
        q     = self.qsfp
        self.sync += beat.eq(beat + 1)
        if n == 4:
            prbs_leds = q.prbs_locked
        else:
            prbs_leds = Cat(*[q.prbs_locked[4*c + 2*h:4*c + 2*h + 2] == 3 for c in range(2) for h in range(2)])
        self.comb += leds.eq(Cat(
            beat[-1],
            q.pll_locked == (2**n - 1),
            q.tx_ready   == (2**n - 1),
            q.rx_ready   == (2**n - 1),
            prbs_leds,
        ))

# Build --------------------------------------------------------------------------------------------

def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=microsoft_storey_peak.Platform, description="QSFP+ transceiver test SoC on Microsoft Storey Peak.")
    parser.add_target_argument("--device",       default="5SGSMD5K2F40I3L", choices=microsoft_storey_peak.Platform.devices, help="Quartus device.")
    parser.add_target_argument("--sys-clk-freq", default=100e6,   type=float, help="System clock frequency (100-125 MHz, it also clocks the transceiver management).")
    parser.add_target_argument("--cages",        default="0",     choices=["0", "1", "01"], help="QSFP cages.")
    parser.add_target_argument("--pll-type",     default=None,    choices=["CMU", "ATX"], help="Transmit PLLs (default: CMU for one cage, ATX for both).")
    parser.add_target_argument("--data-rate",    default=10.3125e9, type=float, help="Lane rate in b/s.")
    parser.add_target_argument("--data-width",   default=80,      type=int, help="PMA direct word width.")
    parser.add_target_argument("--threads",      default=None,    type=int, help="Quartus parallel processors.")
    args = parser.parse_args()

    builder_kwargs = parser.builder_argdict
    output_dir = builder_kwargs.get("output_dir") or os.path.join("build", "microsoft_storey_peak_qsfp_test")
    builder_kwargs["output_dir"] = output_dir

    soc = QSFPTestSoC(
        device       = args.device,
        sys_clk_freq = args.sys_clk_freq,
        cages        = [int(c) for c in args.cages],
        pll_type     = args.pll_type,
        data_rate    = args.data_rate,
        data_width   = args.data_width,
        ip_dir       = os.path.join(output_dir, "ip"),
        threads      = args.threads,
    )
    builder = Builder(soc, **builder_kwargs)
    builder.build(**parser.toolchain_argdict, run=args.build)

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram").replace(".sof", ".rbf"))

if __name__ == "__main__":
    main()
