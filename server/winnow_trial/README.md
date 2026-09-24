# Winnow-12B Q8 HIP 试用

独立试用，跑在 192.168.5.111。官方发布只写了 NVIDIA CUDA 和 Apple Metal。这里的 HIP 构建多做的一件事是把嵌入张量 buffer 从 `CUDA0` 改成 `ROCm0`：该二进制拒绝 `CUDA0`，可用类型是 `CPU` 和 `ROCm0`。决策参数仍是上游 dry-run 的那一组，温度不改，默认 1.0。

进程日志没有「逐层全部 offload」这一行。GPU 上的证据是启动时接受了 `ROCm0`，以及加载后 GTT used 从 30375120896 增到 43952787456。

## 复现

在本机仓库里，脚本目录是 `server/winnow_trial/`。登录用你自己的 SSH，命令里不放密码。

```bash
cd server/winnow_trial
scp -o StrictHostKeyChecking=yes \
  phase1_toolchain.sh phase2_hip.sh build_hip.py download_q8.sh serve.sh stop.sh \
  v@192.168.5.111:/media/v/Data/recordian-winnow-trial/tools/
ssh -o StrictHostKeyChecking=yes v@192.168.5.111 \
  'bash /media/v/Data/recordian-winnow-trial/tools/phase1_toolchain.sh'
```

`phase1_toolchain.sh` 在派生容器里装 Fedora 包。仓库文件 `gpgcheck=1`，`gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-x86_64`（镜像里该文件指向 `RPM-GPG-KEY-fedora-44-primary`，与原版 `fedora.repo` 相同）。不改 AMD `rocm.repo`。2026-09-24 那次实际安装用过 `gpgcheck=0`，RPM 库里 gcc/cmake 的签名头是 `(none)`，phase1 日志有 `skipped OpenPGP checks for 78 packages`。那一次不能算作已经验过签名。现在的脚本只约束以后的安装。

权重和启动：

```bash
ssh -o StrictHostKeyChecking=yes v@192.168.5.111 \
  'bash /media/v/Data/recordian-winnow-trial/tools/download_q8.sh'
ssh -o StrictHostKeyChecking=yes v@192.168.5.111 \
  'bash /media/v/Data/recordian-winnow-trial/tools/serve.sh'
```

`serve.sh` 在创建容器前检查：目录必须正好是 `/media/v/Data/recordian-winnow-trial`；权重大小 12669646592，并且每次启动都实际对该文件跑一次 `sha256sum`，结果必须等于 `b710efc4c0d048ee61eed92c5fef5ce323a4d17e7c51f9f0533cc72ae50818ea`，12.7GB 顺序读一次。同路径的 `.sha256receipt` 不再作为信任依据，只在真实校验通过后按刚读到的字节刷新；同大小替换但不更新 receipt 的文件仍会被拒绝。`MemAvailable` 必须至少还能留下 20GiB，并额外盖住权重字节数和 6GiB 上下文预留。`--memory=28g` 只是容器上限，不算这项。已有同名容器且 `127.0.0.1:42070/health` 为 ok 时，脚本退出且不重建（此时不重新校验权重）。不健康的同名容器也不会被删，要停用下面的停止命令。

## 钉住的版本

| 项 | 值 |
| --- | --- |
| 代码 | `77d14580c6732ca2f3745750c1dc1fd446d8bcee` |
| llama.cpp | `911f6cdc8ab8a530b2bee09ee61471a6f3178eeb` |
| 权重 | `gguf/Winnow-12B-Q8_0.gguf`，12669646592 字节，SHA256 `b710efc4c0d048ee61eed92c5fef5ce323a4d17e7c51f9f0533cc72ae50818ea`，Hub `b6ac22b0d51b69b18200acacb3fbdd98073fffe8` |
| 镜像 | `recordian-winnow-trial:toolchain`，`sha256:3710cf97b1b4d234749b16382ac070f94e1d960bd162d7d7ed9e9b6028319219` |
| 服务容器 | `recordian-winnow-trial`，`2026-09-24T20:15:46Z` 启动，`--restart=no`，端口 `127.0.0.1:42070` 与 `192.168.5.111:42070` |

上下文 4096，一条 decision branch，F16 KV，无视觉投影，`--n-gpu-layers 999`。

## 停止和回滚

```bash
ssh -o StrictHostKeyChecking=yes v@192.168.5.111 \
  'bash /media/v/Data/recordian-winnow-trial/tools/stop.sh'
ssh -o StrictHostKeyChecking=yes v@192.168.5.111 \
  'docker stop recordian-winnow-trial-toolchain; docker rm recordian-winnow-trial-toolchain; docker rmi recordian-winnow-trial:toolchain; rm -rf /media/v/Data/recordian-winnow-trial /home/v/services/recordian-winnow-trial'
```

`stop.sh` 只接受容器名 `recordian-winnow-trial`。不要停 SemIf 或 GPUStack，不要改主机 ROCm 和 `/media/v/Data/llm/models`。
