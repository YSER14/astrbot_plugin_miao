# 预设面板数据（worker/presets）

`worker/presets/<game>/<uid>.json` 是「理论极限/核爆/辅助/平民/毕业/试用」预设玩家数据，
取自 liangshi-calc（MIT）`replace/data/1/PlayerData/`：
https://gitee.com/liangshi233/liangshi-calc （固定提交 `0e10d4f56b991e194d3595aedfeeed91cf08dbfa`）

只做格式搬运，不改数值；缺数据的预设（原神「毕业」、星铁除极限外）不随包分发。
按 MIT 要求保留原作者版权与许可声明，见 liangshi-calc 仓库 LICENSE。

# Reviewed upstream add-ons

`prepare_addons.py` installs the following pinned source trees with their original
LICENSE and README files. Compatibility patches are recorded by that preparer;
the original Yunzai startup/update scripts are not executed.

- Mora: https://gitee.com/Rrrrrrray/mora-plugin, commit
  `e75e320de71d3743bfabae77504ce7c0928aca84`, GPL-3.0. The installed source includes
  its full GPL license. Public guide assets are from its resource repository at
  `85231a2a1dc2ea0c1c67568a97ac3e144e734b72`; its README credits and image-source
  notices are retained. Images do not receive a new license from this adapter.
- Liangshi: https://gitee.com/liangshi233/liangshi-calc, commit
  `0e10d4f56b991e194d3595aedfeeed91cf08dbfa`, MIT, copyright (c) 2024 liangshi.
  This includes the preset data above. The MIT text reproduced below applies.
- Retired achievement resources (withdrawn at the user's request on 2026-11-04,
  no registered handler or new-install dependency): https://github.com/zolay-poi/achievements-plugin, commit
  `256671815c0467284ef34ddc8c7a2db6100ad405`, MIT, copyright (c) 2022 zolay.
  Its catalog/templates and README notices are retained. The MIT text below applies.

The retired achievement adapter used Cocogoat's public browser scanner protocol,
rather than copying or replacing its OCR engine. It is no longer registered.
Protocol reference:
https://github.com/YuehaiTeam/cocogoat/tree/06e2e0081888b49393d9fafb37a9a3a9e286d7c4
(BSD-3-Clause). UIAF interchange format:
https://uigf.org/zh/standards/uiaf.html

# Snap Hutao device compatibility profile

`record-device-profile.json` and the registration payload in `record_device.py`
adapt the pinned `UserFingerprintService.cs` and `HoyolabOptions.cs` from:
https://github.com/SnapHutaoRemasteringProject/Snap.Hutao.Remastered/tree/3b7475ed3eca4aedf9cb81d94741bfbe6fa189f5

MIT License

Copyright (c) 2022-2025 DGP Studio
Copyright (c) 2026 DGP Studio (Snap.Nicole)
Copyright (c) 2026 Snap.HutaoRemasteringProject

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
