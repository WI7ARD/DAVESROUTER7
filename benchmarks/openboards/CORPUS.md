# OpenBoards corpus

60 accepted boards from 46 repositories (suite 1.0.0, generated 2026-10-01T01:01:27Z by `curate.py`). Numbers are `learning.features.board_profile` of the **stripped** board: `signal_layers` = copper layers minus plane-like layers (>60 % zone cover), `long_net_frac` = share of nets whose airwire exceeds a quarter of the board diagonal, `demand` = airwire length per routable area and signal layer.

## Coverage

- copper layers: {2: 27, 4: 22, 6: 4, 8: 5, 12: 2}
- signal (routable) layers: {1: 39, 2: 10, 3: 2, 4: 7, 5: 1, 6: 1}
- with plane layers: 56; all-signal: 4
- domains: {'mcu': 13, 'fpga': 9, 'breakout': 9, 'keyboard': 9, 'power': 5, 'sbc_carrier': 4, 'memory': 4, 'video': 2, 'rf': 2, 'badge': 2, 'led': 1}
- original routing complete (`reference_complete`): 58/60
- selector gaps: 4+ free signal layers without planes: 3; long-net share > 0.8: 4; pad density > 8 /cm²: 38; single routable layer: 39
- licences: {'MIT': 18, 'Apache-2.0': 13, 'CERN-OHL-S-2.0': 12, 'LicenseRef-LoneDynamics-BSD-1-Clause-variant': 6, 'CC-BY-SA-4.0': 4, '0BSD': 2, 'CERN-OHL-1.2': 2, 'CC-BY-4.0': 2, 'SHL-2.1': 1}

## Accepted boards

| ID | Board | Domain | Layers | Signal | Nets | Pads | Pads/cm² | Long-net | Demand | Licence | Ref. complete |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| O001 | `LibreSolar/bms-c1` kicad/bms-c1.kicad_pcb | power | 4 | 1 | 116 | 1344 | 14.2 | 0.19 | 0.279 | CERN-OHL-S-2.0 | yes |
| O002 | `skot/bitaxe` bitaxeUltra.kicad_pcb | power | 4 | 2 | 61 | 444 | 7.9 | 0.25 | 0.166 | CERN-OHL-S-2.0 | yes |
| O003 | `sparkfun/SparkFun_XRP_Controller` Hardware/SparkFun_XRP_Controller.kicad_pcb | power | 6 | 1 | 134 | 683 | 19.9 | 0.38 | 0.727 | MIT | yes |
| O004 | `antmicro/jetson-nano-baseboard` jetson-nano-baseboard.kicad_pcb | sbc_carrier | 8 | 5 | 339 | 1528 | 27.7 | 0.33 | 0.286 | Apache-2.0 | **no** |
| O005 | `antmicro/jetson-orin-baseboard` jetson-orin-baseboard.kicad_pcb | sbc_carrier | 8 | 4 | 634 | 2827 | 39.3 | 0.22 | 0.463 | Apache-2.0 | yes |
| O006 | `antmicro/kria-k26-devboard` kria-k26-devboard.kicad_pcb | sbc_carrier | 8 | 4 | 419 | 2728 | 23.1 | 0.15 | 0.205 | Apache-2.0 | yes |
| O007 | `antmicro/scalenode-cm4-baseboard` scalenode-cm4-baseboard.kicad_pcb | sbc_carrier | 4 | 2 | 132 | 757 | 14.7 | 0.38 | 0.413 | Apache-2.0 | yes |
| O008 | `antmicro/hdmi-mipi-bridge` antmicro-hdmi-mipi-bridge-hw.kicad_pcb | video | 4 | 1 | 105 | 514 | 16.8 | 0.20 | 0.460 | Apache-2.0 | yes |
| O009 | `antmicro/ov9281-camera-board` ov9281-dual-camera-board.kicad_pcb | video | 4 | 2 | 56 | 385 | 15.4 | 0.43 | 0.353 | Apache-2.0 | yes |
| O010 | `antmicro/lpddr4-test-board` lpddr4-test-board.kicad_pcb | memory | 8 | 2 | 314 | 2105 | 21.3 | 0.08 | 0.292 | Apache-2.0 | yes |
| O011 | `antmicro/rdimm-ddr5-tester` data-center-rdimm-ddr5-tester.kicad_pcb | memory | 12 | 6 | 648 | 3422 | 20.2 | 0.23 | 0.208 | Apache-2.0 | yes |
| O012 | `antmicro/sodimm-ddr5-tester` sodimm-ddr5-tester.kicad_pcb | memory | 12 | 4 | 492 | 2745 | 19.4 | 0.12 | 0.239 | Apache-2.0 | **no** |
| O013 | `greatscottgadgets/cynthion-hardware` cynthion.kicad_pcb | fpga | 6 | 1 | 334 | 1472 | 46.9 | 0.09 | 1.086 | CERN-OHL-S-2.0 | yes |
| O014 | `GlasgowEmbedded/glasgow` hardware/boards/glasgow/revC3/glasgow.kicad_pcb | fpga | 4 | 2 | 226 | 1149 | 29.3 | 0.11 | 0.453 | 0BSD | yes |
| O015 | `GlasgowEmbedded/glasgow` hardware/boards/glasgow/revD1/glasgow.kicad_pcb | fpga | 6 | 3 | 561 | 3179 | 60.8 | 0.14 | 0.578 | 0BSD | yes |
| O016 | `gregdavill/butterstick` hardware/ButterStick_r1.0/ButterStick.kicad_pcb | fpga | 8 | 4 | 308 | 1639 | 41.8 | 0.32 | 0.450 | CERN-OHL-1.2 | yes |
| O017 | `tinyvision-ai-inc/pico-ice` Board/Rev3/pico-ice.kicad_pcb | fpga | 4 | 1 | 95 | 455 | 32.7 | 0.43 | 1.241 | MIT | yes |
| O018 | `machdyne/riegel` pcb/riegel_v4/riegel.kicad_pcb | fpga | 4 | 2 | 124 | 473 | 13.1 | 0.17 | 0.301 | LicenseRef-LoneDynamics-BSD-1-Clause-variant | yes |
| O019 | `machdyne/schoko` pcb/schoko_v2/schoko.kicad_pcb | fpga | 4 | 2 | 131 | 647 | 12.9 | 0.16 | 0.303 | LicenseRef-LoneDynamics-BSD-1-Clause-variant | yes |
| O020 | `machdyne/noir` pcb/noir_v1/noir.kicad_pcb | fpga | 6 | 3 | 139 | 762 | 21.8 | 0.16 | 0.243 | LicenseRef-LoneDynamics-BSD-1-Clause-variant | yes |
| O021 | `machdyne/werkzeug` pcb/werkzeug_v3/werkzeug.kicad_pcb | mcu | 2 | 1 | 60 | 249 | 10.0 | 0.20 | 0.311 | LicenseRef-LoneDynamics-BSD-1-Clause-variant | yes |
| O022 | `machdyne/konfekt` pcb/konfekt_v0/konfekt.kicad_pcb | fpga | 4 | 2 | 120 | 668 | 19.1 | 0.25 | 0.336 | LicenseRef-LoneDynamics-BSD-1-Clause-variant | yes |
| O023 | `DangerousPrototypes/BusPirate5-hardware` bus_pirate_pcb/5-REV10A/REV10a.kicad_pcb | mcu | 4 | 4 | 183 | 870 | 13.6 | 0.09 | 0.104 | MIT | yes |
| O024 | `DangerousPrototypes/BusPirate5-hardware` bus_pirate_pcb/6-REV2B/6-REV2b.kicad_pcb | mcu | 4 | 1 | 187 | 927 | 25.5 | 0.21 | 0.823 | MIT | yes |
| O025 | `DangerousPrototypes/BusPirate5-hardware` adapter-flash-sop-1REV3/flash-sop-rev3.kicad_pcb | breakout | 2 | 1 | 13 | 86 | 5.2 | 0.77 | 0.299 | MIT | yes |
| O026 | `greatscottgadgets/hackrf` hardware/hackrf-one/hackrf-one.kicad_pcb | rf | 4 | 2 | 318 | 1639 | 18.2 | 0.13 | 0.394 | CERN-OHL-S-2.0 | yes |
| O027 | `ElectronicCats/CatSniffer` hardware/CatSniffer.kicad_pcb | rf | 4 | 1 | 88 | 418 | 25.8 | 0.19 | 0.725 | CERN-OHL-1.2 | yes |
| O028 | `esp-rs/esp-rust-board` hardware/esp-rust-board/esp-rust-board.kicad_pcb | mcu | 2 | 1 | 34 | 240 | 16.5 | 0.56 | 0.488 | CERN-OHL-S-2.0 | yes |
| O029 | `Sleepdealr/RP2040-designguide` PCB/RP2040-Guide.kicad_pcb | mcu | 2 | 1 | 56 | 203 | 4.8 | 0.54 | 0.358 | MIT | yes |
| O030 | `sparkfun/SparkFun_IoT_RedBoard-RP2350` Hardware/SparkFun_IoT_RedBoard-RP2350.kicad_pcb | mcu | 4 | 1 | 103 | 547 | 13.7 | 0.38 | 0.579 | MIT | yes |
| O031 | `TinyTapeout/tt-demo-pcb` tinytapeout-demo.kicad_pcb | mcu | 4 | 1 | 137 | 877 | 12.1 | 0.42 | 0.606 | Apache-2.0 | yes |
| O032 | `badgeteam/mch2022-badge-hardware` mch2022.kicad_pcb | badge | 4 | 1 | 179 | 1548 | 10.6 | 0.15 | 0.294 | CERN-OHL-S-2.0 | yes |
| O033 | `emfcamp/badge-2024-hardware` tildagon-base/tildagon-base.kicad_pcb | badge | 4 | 1 | 164 | 1203 | 15.6 | 0.40 | 0.680 | CERN-OHL-S-2.0 | yes |
| O034 | `emfcamp/badge-2024-hardware` hexpansion/hexpansion.kicad_pcb | breakout | 2 | 1 | 15 | 64 | 5.2 | 0.93 | 0.287 | CERN-OHL-S-2.0 | yes |
| O035 | `scottbez1/smartknob` electronics/view_base/view_base.kicad_pcb | mcu | 2 | 1 | 57 | 334 | 5.1 | 0.37 | 0.295 | Apache-2.0 | yes |
| O036 | `scottbez1/smartknob` electronics/view_screen/view_screen.kicad_pcb | breakout | 2 | 1 | 11 | 42 | 3.4 | 0.64 | 0.141 | Apache-2.0 | yes |
| O037 | `scottbez1/splitflap` electronics/sensor_smd/sensor_smd.kicad_pcb | breakout | 2 | 1 | 4 | 14 | 1.8 | 0.50 | 0.073 | Apache-2.0 | yes |
| O038 | `VoronDesign/Voron-Hardware` V0_Display_RP2040/KiCad/V0_Display_RP2040.kicad_pcb | mcu | 2 | 1 | 36 | 208 | 7.4 | 0.28 | 0.242 | CC-BY-4.0 | yes |
| O039 | `opulo-inc/lumenpnp` pnp/pcb/mobo/mobo.kicad_pcb | mcu | 4 | 1 | 193 | 1167 | 8.2 | 0.21 | 0.391 | CC-BY-SA-4.0 | yes |
| O040 | `opulo-inc/lumenpnp` pnp/pcb/ring-light/ringLight.kicad_pcb | led | 2 | 1 | 12 | 66 | 3.3 | 0.17 | 0.106 | CC-BY-SA-4.0 | yes |
| O041 | `opulo-inc/lumenpnp` pnp/pcb/ftp/ftp.kicad_pcb | breakout | 2 | 1 | 41 | 169 | 3.0 | 0.02 | 0.074 | CC-BY-SA-4.0 | yes |
| O042 | `hackclub/hackpad` extras/orpheuspad/pcb/orpheuspad_pcb.kicad_pcb | keyboard | 2 | 2 | 18 | 64 | 1.3 | 0.94 | 0.105 | MIT | yes |
| O043 | `davidphilipbarr/Sweep` Sweep Bling MX/pcb/sweep-bling-mx__pcb.kicad_pcb | keyboard | 2 | 1 | 23 | 469 | 4.3 | 0.57 | 0.114 | SHL-2.1 | yes |
| O044 | `foostan/crkbd` pcbs/corne-cherry/hotswap/corne-cherry.kicad_pcb | keyboard | 2 | 1 | 152 | 950 | 3.2 | 0.12 | 0.239 | CC-BY-4.0 | yes |
| O045 | `kata0510/Lily58` Pro_V2/Pro_V2.kicad_pcb | keyboard | 2 | 1 | 212 | 1246 | 3.3 | 0.12 | 0.189 | MIT | yes |
| O046 | `josefadamcik/SofleKeyboard` Sofle_Pico/PCB/Sofle_Pico.kicad_pcb | keyboard | 2 | 1 | 92 | 1687 | 10.0 | 0.24 | 0.202 | MIT | yes |
| O047 | `GEIGEIGEIST/TOTEM` PCB/totem_0-3/totem_0_3.kicad_pcb | keyboard | 2 | 1 | 69 | 528 | 2.2 | 0.55 | 0.223 | CERN-OHL-S-2.0 | yes |
| O048 | `pashutk/chocofi` pcb/chocofi.kicad_pcb | keyboard | 2 | 1 | 44 | 435 | 4.3 | 0.34 | 0.201 | CERN-OHL-S-2.0 | yes |
| O049 | `duckyb/urchin` main.kicad_pcb | keyboard | 2 | 1 | 68 | 570 | 2.9 | 0.26 | 0.114 | MIT | yes |
| O050 | `gtips/reviung` reviung46/pcb/reviung46_ver1_1/reviung46/reviung46.kicad_pcb | keyboard | 2 | 1 | 73 | 541 | 2.0 | 0.25 | 0.192 | MIT | yes |
| O051 | `machdyne/minze` pcb/minze_v1/minze.kicad_pcb | mcu | 4 | 1 | 108 | 593 | 20.8 | 0.24 | 0.639 | LicenseRef-LoneDynamics-BSD-1-Clause-variant | yes |
| O052 | `DangerousPrototypes/BusPirate5-hardware` adapter-flash-dip-1REV3/flash-zif-rev3.kicad_pcb | breakout | 2 | 1 | 13 | 60 | 4.6 | 0.77 | 0.247 | MIT | yes |
| O053 | `DangerousPrototypes/BusPirate5-hardware` adapter-sim-iccard-1REV3C/sim-rev3c.kicad_pcb | breakout | 2 | 1 | 15 | 94 | 4.0 | 0.87 | 0.368 | MIT | yes |
| O054 | `DangerousPrototypes/BusPirate5-hardware` adapter-ir-toy-3REV3/bp5-ir-toy-v3-rev3.kicad_pcb | breakout | 2 | 1 | 19 | 99 | 10.8 | 0.37 | 0.229 | MIT | yes |
| O055 | `DangerousPrototypes/BusPirate5-hardware` adapter-ram-ddr5-1REV2/adapter-ram-ddr5-1REV2.kicad_pcb | memory | 2 | 1 | 19 | 620 | 15.4 | 0.32 | 0.112 | MIT | yes |
| O056 | `opulo-inc/lumenpnp` pnp/pcb/xy-limit/xy-limit.kicad_pcb | breakout | 2 | 1 | 3 | 12 | 0.5 | 1.00 | 0.044 | CC-BY-SA-4.0 | yes |
| O057 | `DangerousPrototypes/BusPirate5-hardware` bus_pirate_pcb/5XL-REV0/5XL-REV0.kicad_pcb | mcu | 4 | 4 | 185 | 876 | 13.7 | 0.09 | 0.104 | MIT | yes |
| O058 | `DangerousPrototypes/BusPirate5-hardware` bus_pirate_pcb/development/7-REV1B-prototype-2/7-REV1B.kicad_pcb | mcu | 4 | 4 | 210 | 1100 | 17.2 | 0.12 | 0.148 | MIT | yes |
| O059 | `Jana-Marie/OtterPill` HW v1.4/OtterPill.kicad_pcb | power | 2 | 1 | 47 | 204 | 26.8 | 0.38 | 0.713 | CERN-OHL-S-2.0 | yes |
| O060 | `Jana-Marie/ligra` ligra_back/ligra_back.kicad_pcb | power | 2 | 1 | 33 | 233 | 2.0 | 0.21 | 0.072 | CERN-OHL-S-2.0 | yes |

## Rejected candidates (18)

| Candidate | Reason |
|---|---|
| `LibreSolar/mppt-2420-hc` kicad/mppt-2420-hc.kicad_pcb | no .kicad_pro project file (KiCad 5 or board-only upload) |
| `LibreSolar/bms-8s50-ic` kicad/bms-8s50-ic.kicad_pcb | no .kicad_pro project file (KiCad 5 or board-only upload) |
| `stuartpittaway/diyBMSv4` ControllerCircuit/ControllerCircuit.kicad_pcb | unknown licence (LICENSE: non-commercial licence) |
| `stuartpittaway/diyBMSv4` ModuleV490-AllInOne/Module_16S.kicad_pcb | unknown licence (LICENSE: non-commercial licence) |
| `OLIMEX/ESP32-POE` HARDWARE/ESP32-PoE-hardware-revision-M2/ESP32-PoE_Rev_M2.kicad_pcb | original routing incomplete (1 connections missing on 1 nets) |
| `DangerousPrototypes/BusPirate5-hardware` adapter-rs232-dual-1REV0/rs232-dual-adapter.kicad_pcb | original routing incomplete (2 connections missing on 2 nets) |
| `joric/nrfmicro` hardware/nrfmicro.kicad_pcb | unknown licence (LICENSE: non-commercial licence) |
| `VoronDesign/Voron-Hardware` Daylight/Daylight_on_a_stick/Daylight.kicad_pcb | unknown licence (LICENSE.md: non-commercial licence) |
| `VoronDesign/Voron-Hardware` SKR-Mini_TFT_Thermistor_Board/KiCad/SKR-Mini_TFT_Thermistor_Board.kicad_pcb | unknown licence (LICENSE.md: non-commercial licence) |
| `VoronDesign/Voron-Hardware` V0-Umbilical/Kicad/Frame_PCB/V0-UmbilicalBoard.kicad_pcb | unknown licence (LICENSE.md: non-commercial licence) |
| `davidphilipbarr/Sweep` Sweep v2.2/sweepv2.kicad_pcb | original routing incomplete (3 connections missing on 3 nets) |
| `diepala/cantor` Cantor_MX/Cantor_MX.kicad_pcb | unknown licence (LICENSE: non-commercial licence) |
| `diepala/cantor` Cantor_Classic/keyboard_pcb.kicad_pcb | unknown licence (LICENSE: non-commercial licence) |
| `beekeeb/piantor` pcb/left/keyboard_pcb.kicad_pcb | unknown licence (LICENSE: non-commercial licence) |
| `DangerousPrototypes/BusPirate5-hardware` adapter-blank-plank-1REV0B/blank-plank-rev0b.kicad_pcb | original routing incomplete (1 connections missing on 1 nets) |
| `emfcamp/badge-2024-hardware` tildagon-2026/tildagon-2026-touch/tildagon-2026-touch.kicad_pcb | unknown licence (LICENSE.txt: unrecognised licence text) |
| `wntrblm/Castor_and_Pollux` hardware/mainboard/mainboard.kicad_pcb | unknown licence (hardware/mainboard/LICENSE: unrecognised licence text) |
| `hydrabus/hydrabus` hardware/HydraBus_1_0_Rev1_5_Kicad/HydraBus.kicad_pcb | no licence file found |
