# 本地项目源码区

该目录用于放置 benchmark 构建和调试时拉取的 GitHub 项目源码。

目录中的源码仓库不进入 git 版本管理。需要研究某个来源项目时，可以在这里 clone：

```bash
git clone https://github.com/builderz-labs/mission-control.git builderz_labs_mission_control
```

正式测试项不依赖该目录中的固定文件。正式 case 通过 `cases/<case_id>/snapshot.json` 记录仓库地址和 commit，测试运行时由 runner 根据 `snapshot.json` 拉取并 checkout 对应版本。
