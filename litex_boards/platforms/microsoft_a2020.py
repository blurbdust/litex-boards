#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2026 blurbdust <blurbdust@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

# Microsoft "A-2020" datacentre FPGA accelerator card.
#   Intel Stratix 10 GX 2800, 1SG280LN2F43E2VG (L-Tile, speed grade 2, FBGA-1760).
#   PCIe x16 edge, 2x QSFP28, 4x DDR4 RDIMM sockets.
#
# Pin assignments are transcribed from two independent public projects that both run
# hardware-verified bitstreams on this board:
#   https://github.com/quake0day/msa2020-stratix10   (PCIe, QSFP0, LEDs, I2C, DDR4 ch0/ch1)
#   https://github.com/SuperSodaSea/Plugcat          (clock, I2C, LEDs, QSFP0, QSFP1)
# Both derive from `Microsoft_A-2020_PINDEF.xlsx`, which is not public. No pin here is inferred.
#
# Confirmed by BOTH sources independently:
#   clk100, i2c, user_led 0-7, qsfp 0 refclk.
# Single source (msa2020):
#   pcie_x16, pcie refclk and perst_n.
# Single source (Plugcat):
#   qsfp 1, the ninth LED, and every _n pin (msa2020 assigns only the positive halves).
# Not available anywhere public, deliberately absent below:
#   DDR4 channels 2 and 3. The card has four DIMM sockets and the vendor spreadsheet covers all
#   four, but only ch0 and ch1 appear in either project. Do not guess them.
#
# Quartus Prime PRO is required; Standard and Lite do not support Stratix 10. Prior art built with
# 23.3 Pro, verified here on 26.1.1 Pro. Stratix 10 also needs a licence, unlike the Stratix V
# boards in this directory.
#
# SAFETY: this device uses SmartVID, so the FPGA negotiates its own core voltage over SDM I2C. The
# PWRMGT assignments in do_finalize() below are mandatory and are taken verbatim from the prior art.
# Both upstream projects warn that a wrong VID configuration can destroy the board. Do not alter
# them without a datasheet in hand.

from litex.build.generic_platform import *
from litex.build.altera import AlteraPlatform
from litex.build.altera.programmer import USBBlaster

# IOs ----------------------------------------------------------------------------------------------

_io = [
    # Clk.
    # 100 MHz user clock. Bank 3D. Plugcat calls it clock_100, msa2020 usr_refclk0; same pin.
    ("clk100", 0, Pins("AD6"), IOStandard("1.8 V")),

    # Leds. Bank 3I. msa2020 names them by function, Plugcat as a flat led[0..8].
    #   0-1 user green, 2-3 user red, 4-7 QSFP status.
    ("user_led", 0, Pins("V6"),  IOStandard("1.8 V")),
    ("user_led", 1, Pins("V7"),  IOStandard("1.8 V")),
    ("user_led", 2, Pins("U7"),  IOStandard("1.8 V")),
    ("user_led", 3, Pins("U8"),  IOStandard("1.8 V")),
    ("user_led", 4, Pins("V8"),  IOStandard("1.8 V")),
    ("user_led", 5, Pins("W8"),  IOStandard("1.8 V")),
    ("user_led", 6, Pins("U9"),  IOStandard("1.8 V")),
    ("user_led", 7, Pins("U10"), IOStandard("1.8 V")),
    # Ninth LED: Plugcat only. msa2020 assigns no ninth LED.
    ("user_led", 8, Pins("V11"), IOStandard("1.8 V")),

    # I2C. Bank 6A. Board-shared bus: DIMM SPD at 0x50, plus the TI DS250DF810 retimers and FPC202.
    ("i2c", 0,
        Subsignal("scl", Pins("AC28")),
        Subsignal("sda", Pins("AB28")),
        IOStandard("3.0-V LVTTL"),
    ),

    # PCIe x16 edge connector.
    # Only the positive pins are given: Quartus infers the negative of a transceiver pair from the
    # package, and msa2020 assigns only the positives too.
    ("pcie_x16", 0,
        Subsignal("rst_n", Pins("AC26"), IOStandard("3.0-V LVTTL")),
        Subsignal("clk_p", Pins("AM34"), IOStandard("HCSL")),
        Subsignal("tx_p",  Pins(
            "BB34 BA36 BB38 AY38 BA40 AV38 AW40 AV42",
            "AU40 AT42 AR40 AP42 AN40 AM42 AL40 AK42")),
        Subsignal("rx_p",  Pins(
            "AV30 AY30 BB30 AW32 BA32 AY34 AU36 AW36",
            "AR36 AN36 AT38 AP38 AL36 AM38 AK38 AJ36")),
    ),

    # QSFP28 cage 0. Bank 1F, transceiver channels 0/4/3/1.
    # NOTE: those channels are NOT contiguous. The upstream project established that this wiring is
    # intended for 4x25G and that bonded 100G (CAUI-4) provably cannot be placed, because the single
    # refclk at AD34 cannot reach both channel triplets.
    ("qsfp", 0,
        Subsignal("refclk_p", Pins("AD34"), IOStandard("HCSL")),
        Subsignal("refclk_n", Pins("AD33"), IOStandard("HCSL")),
        Subsignal("tx_p", Pins("AG40 AC40 AD42 AF42")),
        Subsignal("tx_n", Pins("AG39 AC39 AD41 AF41")),
        Subsignal("rx_p", Pins("AG36 AC36 AB38 AD38")),
        Subsignal("rx_n", Pins("AG35 AC35 AB37 AD37")),
        IOStandard("HIGH SPEED DIFFERENTIAL I/O"),
    ),

    # QSFP28 cage 1. Bank 1N, transceiver channels 0/4/3/1. Plugcat is the only source for these.
    ("qsfp", 1,
        Subsignal("refclk_p", Pins("H34"), IOStandard("HCSL")),
        Subsignal("refclk_n", Pins("H33"), IOStandard("HCSL")),
        Subsignal("tx_p", Pins("F38 A36 B38 C40")),
        Subsignal("tx_n", Pins("F37 A35 B37 C39")),
        Subsignal("rx_p", Pins("C32 D30 A28 B30")),
        Subsignal("rx_n", Pins("C31 D29 A27 B29")),
        IOStandard("HIGH SPEED DIFFERENTIAL I/O"),
    ),
]

# Platform -----------------------------------------------------------------------------------------

class Platform(AlteraPlatform):
    default_clk_name   = "clk100"
    default_clk_period = 1e9/100e6

    # quartus_pfg cannot emit an SVF for this device:
    #   Error (20718): Serial Vector Format file (.svf) generation for the target device
    #                  1SG280LN2F43 is not supported
    # Without this the build returns non-zero after producing a perfectly good .sof and .rbf.
    create_svf = False

    def __init__(self, toolchain="quartus"):
        AlteraPlatform.__init__(self, "1SG280LN2F43E2VG", _io, toolchain=toolchain)

    def create_programmer(self):
        # openFPGALoader has no Stratix 10 entries at all, so programming goes through Quartus.
        # The board's onboard FT232H additionally uses a Microsoft-specific Quartus JTAG driver
        # (jtag_hw_microsoft_catapult), which supports programming and simple shifts but hangs on
        # System Console JTAG-Avalon streaming.
        return USBBlaster()

    def do_finalize(self, fragment):
        AlteraPlatform.do_finalize(self, fragment)
        self.add_period_constraint(self.lookup_request("clk100", loose=True), 1e9/100e6)

        # Stratix 10 needs FAMILY stated explicitly; LiteX emits only DEVICE.
        self.add_platform_command('set_global_assignment -name FAMILY "Stratix 10"')

        # SmartVID. Mandatory on this device, and taken verbatim from the upstream projects.
        # A wrong configuration here can destroy the board.
        for cmd in [
            'set_global_assignment -name PWRMGT_SLAVE_DEVICE_TYPE OTHER',
            'set_global_assignment -name PWRMGT_SLAVE_DEVICE0_ADDRESS 60',
            'set_global_assignment -name PWRMGT_VOLTAGE_OUTPUT_FORMAT "DIRECT FORMAT"',
            'set_global_assignment -name PWRMGT_DIRECT_FORMAT_COEFFICIENT_M 1',
            'set_global_assignment -name PWRMGT_TRANSLATED_VOLTAGE_VALUE_UNIT MILLIVOLTS',
            'set_global_assignment -name USE_PWRMGT_SCL SDM_IO14',
            'set_global_assignment -name USE_PWRMGT_SDA SDM_IO11',
            # Configuration: active serial x4 from the onboard flash.
            'set_global_assignment -name USE_CONF_DONE SDM_IO16',
            'set_global_assignment -name USE_INIT_DONE SDM_IO0',
            # USE_CVP_CONFDONE is deliberately NOT set. Configuration via Protocol claims the pin
            # and interferes with JTAG programming on this board. Every PCIe project in the prior
            # art omits it and says so explicitly; only their non-PCIe blink design sets it.
        ]:
            self.add_platform_command(cmd)
