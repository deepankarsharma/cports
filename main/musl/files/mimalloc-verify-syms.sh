#!/bin/sh

nm "$1" | grep '[0-9A-Za-z] [A-Z] ' | while read -r addr type name; do
    case "$name" in
        # glue symbols
        __libc_*|__malloc_*) ;;
        # compiler-generated
        .L*) ;;
        # directly provided api
        aligned_alloc|malloc_usable_size) ;;
        # mimalloc heaps
        _mi_heap_empty|_mi_heap_main) ;;
        # XRay shared-DSO safety stubs (weak; see mimalloc.c)
        __xray_register_dso|__xray_deregister_dso) ;;
        _ZN6__xray*) ;;
        *)
            echo "unexpected symbol $name ($type)"
            exit 1
            ;;
    esac
done
