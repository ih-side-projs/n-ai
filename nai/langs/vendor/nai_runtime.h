/* Minimal runtime support for the C adapter.
 *
 * Vendored in the nai repository and copied into the workspace by the adapter.
 * ``NOT_IMPLEMENTED(name)`` prints "not implemented: <name>" to stderr and
 * aborts, so a contract stub fails loudly until its body is implemented.
 */
#ifndef NAI_RUNTIME_H
#define NAI_RUNTIME_H

#include <stdio.h>
#include <stdlib.h>

#define NOT_IMPLEMENTED(name) \
    do { \
        fprintf(stderr, "not implemented: %s\n", (name)); \
        abort(); \
    } while (0)

#endif /* NAI_RUNTIME_H */
