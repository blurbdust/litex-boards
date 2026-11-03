#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2026 blurbdust <blurbdust@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

# Bring-up SoC for the Microsoft/HP "Storey Peak" Catapult v2 card (P/N X930613-001).
#
# There is no hardware UART on this board, so as with Catapult v3 the console falls back to
# jtag_uart, or to crossover when jtagbone is enabled.
#
# The DDR3L is main RAM, through Intel's UniPHY controller (Quartus generates it at build time). The
# PCIe and QSFP+ pins are declared in the platform file but are not driven here.

from migen import *

from litex_boards.platforms import microsoft_storey_peak

from litex.gen import *

from litex.soc.cores.clock.intel_stratix5 import StratixVPLL
from litex.soc.integration.soc import *
from litex.soc.integration.builder import *
from litex.soc.cores.led import LedChaser
from litex.soc.cores.bitbang import I2CMaster
from litex.soc.cores.ram.altera_uniphy_ddr3 import AlteraUniPHYDDR3, add_altera_uniphy_ddr3

# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq):
        self.rst    = Signal()
        self.cd_sys = ClockDomain()

        # # #

        # Clk / Rst.
        # No reset input exists on this board and none appears anywhere in the reference
        # design's pinout, so sys reset comes from the PLL lock only.
        clk125 = platform.request("clk125")

        # PLL.
        # speedgrade matches the C1 suffix of the 5SGSMD5K1F40C1 device string.
        self.pll = pll = StratixVPLL(speedgrade="-C1")
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(clk125, 125e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)

        # sys and clk125 are related only through the PLL. The SoC reset (sys domain) reaches
        # registers clocked by clk125 as an asynchronous reset, which STA otherwise times as a
        # synchronous CDC and reports as a setup violation.
        platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=100e6,
        with_led_chaser = True,
        with_i2c        = True,
        **kwargs):
        self.platform = platform = microsoft_storey_peak.Platform()

        # CRG --------------------------------------------------------------------------------------
        self.crg = _CRG(platform, sys_clk_freq)

        # SoCCore ----------------------------------------------------------------------------------
        # Defaults to JTAG-UART since no hardware UART.
        if kwargs.get("uart_name", "serial") == "serial":
            kwargs["uart_name"] = "crossover" if kwargs.get("with_jtagbone", False) else "jtag_uart"
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on Microsoft Storey Peak", **kwargs)

        # JTAG -------------------------------------------------------------------------------------
        # Both jtag_uart and jtagbone run off the TAP's altera_reserved_tck, which is otherwise an
        # unconstrained clock (STA reports it as such and leaves that logic untimed). Constrain it
        # conservatively and declare it asynchronous to everything else.
        platform.toolchain.additional_sdc_commands += [
            "create_clock -name altera_reserved_tck -period 100.000 [get_ports {altera_reserved_tck}]",
            "set_clock_groups -asynchronous -group {altera_reserved_tck}",
        ]

        # DDR3L SDRAM ------------------------------------------------------------------------------
        # 72-bit (64-bit + ECC), from clk125 (in the DDR3 bank). The controller runs on its own clock
        # (200 MHz AFI clock at 800 MHz, quarter rate), asynchronous to sys.
        if not self.integrated_main_ram_size:
            self.ddr3 = AlteraUniPHYDDR3(platform,
                pads         = platform.request("ddram_ecc"),
                rzq_pad      = platform.request("ddram_oct"),
                refclk       = platform.lookup_request("clk125"),
                refclk_freq  = 125e6,
                mem_clk_freq = platform.ddr_clk_freq,
                ecc          = True,
                config       = platform.ddr_config,
            )
            add_altera_uniphy_ddr3(self, self.ddr3, size=platform.ddr_size)

        # JTAGBone ---------------------------------------------------------------------------------
        if kwargs.get("with_jtagbone", False):
            platform.add_period_constraint(self.jtagbone_phy.cd_jtag.clk, 1e9/20e6)
            platform.add_false_path_constraints(self.jtagbone_phy.cd_jtag.clk, self.crg.cd_sys.clk)

        # Leds -------------------------------------------------------------------------------------
        if with_led_chaser:
            self.leds = LedChaser(
                pads         = platform.request_all("user_led"),
                sys_clk_freq = sys_clk_freq)

        # I2C --------------------------------------------------------------------------------------
        # i2c0 reaches the IDT clock generator, which is the part to poke at if clk125 turns out not
        # to be free-running. The rest are QSFP0/QSFP1/board-monitor.
        if with_i2c:
            self.i2c0 = I2CMaster(platform.request("i2c", 0))
            self.i2c1 = I2CMaster(platform.request("i2c", 1))
            self.i2c2 = I2CMaster(platform.request("i2c", 2))
            self.i2c3 = I2CMaster(platform.request("i2c", 3))

# Build --------------------------------------------------------------------------------------------

def main():
    from litex.build.parser import LiteXArgumentParser
    parser = LiteXArgumentParser(platform=microsoft_storey_peak.Platform, description="LiteX SoC on Microsoft Storey Peak.")
    parser.add_target_argument("--sys-clk-freq",    default=100e6, type=float,         help="System clock frequency.")
    parser.add_target_argument("--with-led-chaser", action="store_true", default=True,  help="Enable LED Chaser.")
    parser.add_target_argument("--no-led-chaser", dest="with_led_chaser", action="store_false", help="Disable LED Chaser.")
    parser.add_target_argument("--with-i2c",        action="store_true", default=True,  help="Enable I2C masters.")
    parser.add_target_argument("--no-i2c", dest="with_i2c", action="store_false", help="Disable I2C masters.")
    args = parser.parse_args()

    soc = BaseSoC(
        sys_clk_freq    = args.sys_clk_freq,
        with_led_chaser = args.with_led_chaser,
        with_i2c        = args.with_i2c,
        **parser.soc_argdict
    )
    builder = Builder(soc, **parser.builder_argdict)
    if args.build:
        builder.build(**parser.toolchain_argdict)

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram").replace(".sof", ".rbf"))

if __name__ == "__main__":
    main()
