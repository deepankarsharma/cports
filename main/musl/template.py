pkgname = "musl"
pkgver = "1.2.6"
pkgrel = 4
_commit = "9fa28ece75d8a2191de7c5bb53bed224c5947417"
_mimalloc_ver = "2.2.7"
build_style = "gnu_configure"
configure_args = ["--prefix=/usr", "--disable-gcc-wrapper"]
configure_gen = []
make_build_args = []
depends = [self.with_pkgver("musl-progs")]
provides = ["so:libc.so=0"]
provider_priority = 999
replaces = [f"musl-mallocng~{pkgver}"]
pkgdesc = "Musl C library"
license = "MIT"
url = "http://www.musl-libc.org"
source = [
    f"https://git.musl-libc.org/cgit/musl/snapshot/musl-{_commit}.tar.gz",
    f"https://github.com/microsoft/mimalloc/archive/refs/tags/v{_mimalloc_ver}.tar.gz",
]
source_paths = [".", "mimalloc"]
sha256 = [
    "d3baf222d234f2121e71b7eabd0c17667b7a3733b3077e99f9920c69cb5899df",
    "8e0ed89907a681276bff2e49e9a048b47ba51254ab60daf6b3c220acac456a95",
]
compression = "deflate"
# scp makes it segfault
hardening = ["!scp"]
# does not ship tests
options = ["bootstrap", "!check", "!lto"]

# whether to use musl's stock allocator
# for now 32-bit targets until we patch out 64-bit atomics in arena
_use_mng = self.profile().wordsize == 32

if _use_mng:
    configure_args += ["--with-malloc=mallocng"]
else:
    configure_args += ["--with-malloc=external"]
    make_build_args += ["EXTRA_OBJ=$(srcdir)/src/malloc/external/mimalloc.o"]

if self.stage > 0:
    # have base-files extract first in normal installations
    #
    # don't do this for stage 0 though, because otherwise base-files will
    # get installed as a makedepend and subsequently removed as an autodep,
    # which will nuke the base symlinks handled by initial initdb, as the
    # stage0 bldroot is not a complete chroot and relies on the external
    # state we give it during first setup
    #
    # but this only really matters for "real" systems, so in stage 0 we can
    # just avoid the dependency and work around the whole issue
    #
    depends += ["base-files"]


def post_extract(self):
    # reported in libc.so --version
    with open(self.cwd / "VERSION", "w") as f:
        f.write(pkgver)
    # copy in our mimalloc unified source
    self.cp(self.files_path / "mimalloc-verify-syms.sh", ".")
    self.cp(self.files_path / "mimalloc.c", "mimalloc/src")
    # XRay attr-list: forces sleds onto the mman + mem-op entry points (see
    # pre_configure). Lives at the source root so $(srcdir)/xray-attr-list.txt
    # resolves during the build.
    self.cp(self.files_path / "xray-attr-list.txt", ".")
    # now we're ready to get patched
    # but also remove musl's x86_64 asm mem-ops so the C implementations are
    # built instead: memcpy/memmove because the C versions are actually
    # noticeably faster, and memset additionally so it can carry XRay sleds
    # (the attr-list force-instruments the C definitions, not the asm).
    self.rm("src/string/x86_64/memcpy.s")
    self.rm("src/string/x86_64/memmove.s")
    self.rm("src/string/x86_64/memset.s")


def init_configure(self):
    # ensure that even early musl uses compiler-rt
    if self.stage == 0:
        self.env["LIBCC_LDFLAGS"] = "--rtlib=compiler-rt"
        return


def pre_configure(self):
    # Instrument libc with LLVM XRay so the allocator / mman / mem-op entry
    # points carry patchable sleds. Two complementary mechanisms select what
    # gets a sled while leaving everything else (notably the startup path)
    # clean:
    #   - source-level __attribute__((xray_always_instrument)) on the mimalloc
    #     allocator entry points (see files/mimalloc.c);
    #   - the attr-list (files/xray-attr-list.txt) for the mman + mem-op
    #     functions that live in musl proper (__mmap, memcpy, ...).
    # A very high instruction threshold plus -fxray-ignore-loops makes the
    # size/loop heuristics never auto-select anything, so ONLY the two lists
    # above are instrumented.
    #
    # -fxray-shared makes libc.so a registrable XRay DSO: a load-time
    # constructor registers its sled map with whatever XRay runtime lives in
    # the executable, so an instrumented program's __xray_patch() can patch
    # libc's sleds cross-image. The matching libclang_rt.xray-dso runtime is
    # added to the libc.so link below; its (and the sleds') references to the
    # XRay runtime's registration entry points / handler globals are satisfied
    # by the WEAK stubs in files/mimalloc.c, so non-XRay programs (which is
    # every normal binary, since they all link libc.so) load fine with the
    # sleds inert.
    #
    # Only relevant for the external (mimalloc) allocator path; the 32-bit
    # mallocng path is left untouched.
    if _use_mng:
        return

    xray_cflags = (
        " -fxray-instrument -fxray-shared"
        " -fxray-instruction-threshold=1000000 -fxray-ignore-loops"
        " -fxray-attr-list=$(srcdir)/xray-attr-list.txt"
    )

    mf = self.cwd / "Makefile"
    data = mf.read_text()

    # 1) Add the XRay flags to the global compile flags. CFLAGS_ALL is used by
    #    every compile rule (including the special mimalloc one), so this
    #    instruments all of libc uniformly.
    cflags_needle = "CFLAGS_ALL += $(CPPFLAGS) $(CFLAGS_AUTO) $(CFLAGS)"
    if data.count(cflags_needle) != 1:
        self.error("could not locate CFLAGS_ALL definition to add XRay flags")
    data = data.replace(
        cflags_needle,
        cflags_needle + "\nCFLAGS_ALL +=" + xray_cflags,
    )

    # 2) Link the XRay DSO runtime into libc.so (it provides the sled
    #    trampolines and the registration constructor). The link uses
    #    -nostdlib, so clang will not pull it in automatically; add it by the
    #    path clang reports. Its undefined runtime references are satisfied by
    #    the weak stubs in mimalloc.o, so --no-undefined stays happy.
    link_needle = "-Wl,-e,_dlstart -o $@ $(LOBJS) $(LDSO_OBJS) $(LIBCC)"
    if data.count(link_needle) != 1:
        self.error("could not locate libc.so link rule to add XRay runtime")
    data = data.replace(
        link_needle,
        "-Wl,-e,_dlstart -o $@ $(LOBJS) $(LDSO_OBJS) $(LIBCC)"
        " $(shell $(CC) -print-file-name=libclang_rt.xray-dso.a)",
    )

    mf.write_text(data)

    # 3) musl links libc.so with --dynamic-list, which makes every symbol NOT
    #    on the list bind *locally* (non-preemptible). Left alone, libc.so's
    #    own reference to __xray_register_dso would bind to its weak no-op stub
    #    instead of the strong definition an XRay executable exports, so the
    #    DSO would never register and cross-image patching would silently do
    #    nothing. Add the XRay registration symbols to the dynamic list so they
    #    stay preemptible: an instrumented executable's runtime then overrides
    #    the stubs, while normal programs still fall back to the weak no-ops.
    dl = self.cwd / "dynamic.list"
    dl_data = dl.read_text()
    dl_needle = "__stack_chk_guard;\n};"
    if dl_data.count(dl_needle) != 1:
        self.error("could not locate dynamic.list tail to add XRay symbols")
    dl_data = dl_data.replace(
        dl_needle,
        "__stack_chk_guard;\n\n"
        "__xray_register_dso;\n"
        "__xray_deregister_dso;\n"
        "_ZN6__xray19XRayPatchedFunctionE;\n"
        "_ZN6__xray13XRayArgLoggerE;\n"
        "_ZN6__xray21XRayPatchedTypedEventE;\n"
        "_ZN6__xray22XRayPatchedCustomEventE;\n};",
    )
    dl.write_text(dl_data)


def post_build(self):
    from cbuild.util import compiler

    self.cp(self.files_path / "getent.c", ".")
    self.cp(self.files_path / "getconf.c", ".")
    self.cp(self.files_path / "iconv.c", ".")
    self.cp(self.files_path / "__stack_chk_fail_local.c", ".")

    cc = compiler.C(self)

    cc.invoke(["getent.c"], "getent")
    cc.invoke(["getconf.c"], "getconf")
    cc.invoke(["iconv.c"], "iconv")

    cc.invoke(
        ["__stack_chk_fail_local.c"],
        "__stack_chk_fail_local.o",
        obj_file=True,
    )
    self.do(
        self.get_tool("AR"),
        "r",
        "libssp_nonshared.a",
        "__stack_chk_fail_local.o",
    )


def pre_install(self):
    self.install_dir("usr/lib")
    # ensure all files go in /usr/lib
    self.install_link("lib", "usr/lib")

    self.install_license("COPYRIGHT")


def post_install(self):
    # no need for the symlink anymore
    self.uninstall("lib")

    # fix up ld-musl-whatever so it does not point to absolute path
    for f in (self.destdir / "usr/lib").glob("ld-musl-*.so.1"):
        f.unlink()
        f.symlink_to("libc.so")

    self.install_dir("usr/bin")
    self.install_link("usr/bin/ldd", "../lib/libc.so")

    self.install_bin("iconv")
    self.install_bin("getent")
    self.install_bin("getconf")

    self.install_file("libssp_nonshared.a", "usr/lib")

    self.install_man(self.files_path / "getent.1")
    self.install_man(self.files_path / "getconf.1")

    self.install_link("usr/bin/ldconfig", "true")


@subpackage("musl-progs")
def _(self):
    # we can't have a versioned symlink dep on musl
    self.options = ["brokenlinks", "!scanrundeps"]
    self.depends = ["so:libc.so!musl"]
    return self.default_progs()


@subpackage("musl-devel-static")
def _(self):
    return ["usr/lib/libc.a"]


@subpackage("musl-libssp-static")
def _(self):
    self.subdesc = "libssp_nonshared for some targets"
    self.depends = []

    return ["usr/lib/libssp_nonshared.a"]


@subpackage("musl-devel")
def _(self):
    # empty depends so libc.so can be switched with alternatives
    # the libc itself installs as a solib dep of everything anyway
    self.depends = []
    self.options = ["!splitstatic"]
    # the .a files are empty archives
    return ["usr/include", "usr/lib/*.o", "usr/lib/*.a"]
