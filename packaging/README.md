# key2touch — Fedora RPM packaging / Fedora 打包

## EN — rebuild locally

Upstream publishes no release tarballs yet, so Source0 is a git snapshot of the
commit pinned as `%commit` in `key2touch.spec`:

```sh
# 1. Produce Source0 (the --prefix must match what %setup expects):
git archive --prefix=key2touch-20261104/ e3272fe | gzip > key2touch-20261104.tar.gz

# 2. Hand it to rpmbuild:
mkdir -p ~/rpmbuild/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
cp key2touch-20261104.tar.gz ~/rpmbuild/SOURCES/
rpmbuild -bb packaging/key2touch.spec

# 3. Or build inside a Fedora 45 container (build.sh lives in your rpmbuild tree):
podman run -v $PWD/rpmbuild-tree:/builddir/k2tbuild:Z \
  registry.fedoraproject.org/fedora:45 /builddir/k2tbuild/build.sh
```

Install / remove: `sudo dnf install ~/rpmbuild/RPMS/noarch/key2touch-*.rpm`,
`sudo dnf remove key2touch`.  Access to `/dev/uinput` needs the `input` group;
the GUI repairs that through `pkexec`.

## zh — 本地重新构建

上游暂无正式版本包，Source0 是 spec 里 `%commit` 指定提交的 git 快照：

1. `git archive --prefix=key2touch-20261104/ e3272fe` 生成 `key2touch-20261104.tar.gz`
   （`--prefix` 必须和 `%setup` 期望的目录名一致）。
2. 压缩包放进 `~/rpmbuild/SOURCES/`，执行 `rpmbuild -bb packaging/key2touch.spec`。
3. 或在 Fedora 45 容器内构建：直接运行上面的 podman 命令，`build.sh` 由你的
   `rpmbuild-tree` 提供。

安装：`sudo dnf install ~/rpmbuild/RPMS/noarch/key2touch-*.rpm`。访问 `/dev/uinput`
需要 `input` 组权限，应用内会用 `pkexec` 引导修复。
