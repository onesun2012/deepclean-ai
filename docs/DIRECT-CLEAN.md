# 直接释放空间策略

2026-09-08：普通清理仍默认请求回收站；新增独立的永久删除选项，只处理以下目录里至少 7 天未修改的文件。其他目录不因 JSON 标注 `safe` 或 `rebuildable` 而取得永久删除资格。

| 分项 | 白名单目录 | 影响 |
|---|---|---|
| 临时文件 | `%LOCALAPPDATA%\Temp` | 旧文件不一定无用，先结束安装和其他任务；不包括 Windows Temp |
| pip / uv | `%LOCALAPPDATA%\pip\Cache\http`、`http-v2` | 下次安装可能重新下载；保留 wheels、uv 和已安装环境 |
| npm / pnpm | `%LOCALAPPDATA%\npm-cache\_cacache`、`%USERPROFILE%\.npm\_cacache` | 下次安装可能重新下载；不处理 pnpm store |
| NuGet | `%LOCALAPPDATA%\NuGet\v3-cache` | 还原时可能重新下载；保留全局包目录 |
| Electron | `%LOCALAPPDATA%\electron\Cache` | 再安装可能重新下载二进制；保留 electron-builder 目录 |

删除缓存可能消耗重新下载的时间和流量；离线、私有源凭据失效或上游删除版本时可能无法恢复。请先结束相关安装和构建任务。永久删除不进入回收站，不是安全擦除，也不代表同等数量的磁盘空间一定立即增加。结果独立读取磁盘可用空间，其变化可能受其他进程影响。

确认框逐项显示后端筛选后的大小、实际白名单目录与影响；不支持的已选项不参与，符合条件但被占用的文件仍可能在执行时跳过。网页五分钟一次性计划绑定删除模式，执行必须明确提交 `confirm_permanent: true`。CLI 需要 `--permanent --yes`，不带 `--yes` 返回计划与影响。

执行再次复核目录、保留期、扫描身份和保护路径；通过独占 Windows 文件句柄打开同一文件，核对 `fstat` 身份和最终路径，再用 `SetFileInformationByHandle(FileDispositionInfo)` 标记删除并关闭句柄。不允许共享访问，已打开文件可能导致独占失败并跳过；不回退 `os.remove`。仅删除文件，保留空目录。管理员实例禁止实际清理。

没有开放自定义宽泛路径、Downloads 全目录、Windows 更新、Installer、WinSxS、模型或已安装组件的永久删除。年龄不能证明安装维护文件可删；Windows 更新和旧安装文件由系统清理功能处理。原有 Shell 回收路径的最终竞态仍是独立的已知限制，不能据此宣称所有清理操作都已原子化。

范围依据（官方文档）：

- [pip 缓存机制](https://pip.pypa.io/en/stable/topics/caching/)：HTTP 缓存与 wheel 缓存分开处理。
- [npm cache](https://docs.npmjs.com/cli/v7/commands/npm-cache/)：`_cacache` 存储缓存内容，缓存可重新获取。
- [NuGet 缓存目录](https://learn.microsoft.com/en-us/nuget/consume-packages/managing-the-global-packages-and-cache-folders)：HTTP 缓存与全局包目录用途不同。
- [Electron 安装说明](https://github.com/electron/electron/blob/main/docs/tutorial/installation.md)：Windows 下载缓存位于 `electron/Cache`。
- [Windows 文件句柄删除接口](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)：文件删除信息需要 DELETE 访问权限。
- [Windows Installer 缓存缺失风险](https://learn.microsoft.com/en-us/troubleshoot/windows-client/application-management/missing-windows-installer-cache)：维护缓存缺失会影响修复、更新或卸载。
