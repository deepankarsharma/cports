/* The Chimera Linux unified mimalloc configuration. */

/* enable our changes */
#define MI_LIBC_BUILD 1
/* the libc malloc should not read any env vars */
#define MI_NO_GETENV 1
/* disable process constructor stuff */
#define MI_PRIM_HAS_PROCESS_ATTACH 1
/* reduce virt memory usage */
#define MI_DEFAULT_ARENA_RESERVE 64L*1024L
/* this is a hardened build */
#define MI_SECURE 4
/* this would be nice to have, but unfortunately it
 * makes some things a lot slower (e.g. sort(1) becomes
 * roughly 2.5x slower) so disable unless we figure out
 * some way to make it acceptable...
 */
#define MI_PADDING 0

/* use smaller segments to accommodate smaller arenas */
#define MI_SEGMENT_SHIFT (7 + MI_SEGMENT_SLICE_SHIFT)

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"

#include <features.h>
/* small workaround for musl includes */
#ifdef weak
#undef weak
#endif

#include "pthread_impl.h"

/* since we are internal we can make syscalls more direct (via macros) */
#include "syscall.h"
#define madvise __madvise
#define MADV_DONTNEED POSIX_MADV_DONTNEED

/* some verification whether we can make a valid build */
#include <stdatomic.h>

#if ATOMIC_LONG_LOCK_FREE != 2 || ATOMIC_CHAR_LOCK_FREE != 2
#error Words and bytes must always be lock-free in this context
#endif

/* arena purge timing stuff (may fix later), stats (can patch out) */
#if ATOMIC_LLONG_LOCK_FREE != 2
#error 64-bit atomics must be lock-free for now
#endif

/* the whole mimalloc source */
#include "static.c"

/* chimera entrypoints */

#define INTERFACE __attribute__((visibility("default")))

#ifndef __has_attribute
#define __has_attribute(x) 0
#endif

/* XRAY_MALLOC_BYTES: instrument + log the first argument (the requested
 * size) so the handler can do per-allocation byte accounting. Only malloc's
 * size is its first ABI argument, so only it logs bytes.
 * XRAY_TRACE: instrument for count + timing only (free/calloc/realloc/
 * aligned_alloc, whose size is not arg1 or is split across two args). */
#if __has_attribute(xray_always_instrument) && __has_attribute(xray_log_args)
#define XRAY_MALLOC_BYTES \
	__attribute__((xray_always_instrument, xray_log_args(1), noinline))
#define XRAY_TRACE __attribute__((xray_always_instrument, noinline))
#else
#define XRAY_MALLOC_BYTES
#define XRAY_TRACE
#endif

extern int __malloc_replaced;
extern int __aligned_alloc_replaced;

void * const __malloc_tls_default = (void *)&_mi_heap_empty;

void __malloc_init(pthread_t p) {
    _mi_auto_process_init();
}

void __malloc_tls_teardown(pthread_t p) {
    /* if we never allocated on it, don't do anything */
    if (p->malloc_tls == (void *)&_mi_heap_empty)
        return;
    /* otherwise finalize the thread and reset */
    _mi_thread_done(p->malloc_tls);
    p->malloc_tls = (void *)&_mi_heap_empty;
}

/* we have nothing to do here, mimalloc is lock-free */
void __malloc_atfork(int who) {
    if (who < 0) {
        /* disable */
    } else {
        /* enable */
    }
}

/* we have no way to implement this AFAICT */
void __malloc_donate(char *a, char *b) { (void)a; (void)b; }

XRAY_TRACE void *__libc_calloc(size_t m, size_t n) {
    return mi_calloc(m, n);
}

XRAY_TRACE void __libc_free(void *ptr) {
    mi_free(ptr);
}

XRAY_MALLOC_BYTES void *__libc_malloc_impl(size_t len) {
    return mi_malloc(len);
}

XRAY_TRACE void *__libc_realloc(void *ptr, size_t len) {
    return mi_realloc(ptr, len);
}

/* technically mi_aligned_alloc and mi_memalign are the same in mimalloc
 * which is good for us because musl implements memalign with aligned_alloc
 */
XRAY_TRACE INTERFACE void *aligned_alloc(size_t align, size_t len) {
    if (mi_unlikely(__malloc_replaced && !__aligned_alloc_replaced)) {
        errno = ENOMEM;
        return NULL;
    }
    void *p = mi_malloc_aligned(len, align);
    mi_assert_internal(((uintptr_t)p % align) == 0);
    return p;
}

INTERFACE size_t malloc_usable_size(void *p) {
    return mi_usable_size(p);
}

/* --------------------------------------------------------------------------
 * XRay shared-DSO safety stubs.
 *
 * Compiling libc with -fxray-instrument -fxray-shared makes the instrumented
 * functions (and the linked libclang_rt.xray-dso runtime) emit *strong*
 * undefined references to the XRay runtime's registration entry points and
 * patched-handler globals. libc.so is loaded by every program on the system,
 * so strong undefs would make every non-XRay binary fail to link/load — it
 * would brick the whole system.
 *
 * We defuse that by providing WEAK, default-visibility definitions of exactly
 * those symbols here (mimalloc.o is already linked into libc.so and compiled
 * with the XRay flags, so this is the natural home). In a normal program the
 * weak stubs win: registration is a no-op and the sleds stay unpatched/inert.
 * In an XRay-instrumented executable, the program links libclang_rt.xray.a
 * (which defines these symbols *strongly*) and exports them with
 * -Wl,--export-dynamic-symbol, so libc.so's weak refs bind to the real
 * runtime at load time and cross-image patching works.
 *
 * The C++-mangled names are pinned with __asm__ so they match the runtime's
 * symbols exactly; visibility("default") overrides the -fvisibility=hidden the
 * mimalloc translation unit is built with.
 */
#if __has_attribute(xray_always_instrument)
#define XRAY_WEAK __attribute__((weak, visibility("default")))

XRAY_WEAK void *XRayPatchedFunction_stub
    __asm__("_ZN6__xray19XRayPatchedFunctionE") = 0;
XRAY_WEAK void *XRayArgLogger_stub
    __asm__("_ZN6__xray13XRayArgLoggerE") = 0;
XRAY_WEAK void *XRayPatchedTypedEvent_stub
    __asm__("_ZN6__xray21XRayPatchedTypedEventE") = 0;
XRAY_WEAK void *XRayPatchedCustomEvent_stub
    __asm__("_ZN6__xray22XRayPatchedCustomEventE") = 0;

XRAY_WEAK int __xray_register_dso(void) { return 0; }
XRAY_WEAK int __xray_deregister_dso(void) { return 0; }

#undef XRAY_WEAK
#endif
