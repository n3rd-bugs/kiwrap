/* LD_PRELOAD shim (spike only): log + neuter attempts to pin GDK to x11. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
void gdk_set_allowed_backends(const char *backends) { fprintf(stderr, "[shim] gdk_set_allowed_backends(%s) ignored\n", backends); }
int setenv(const char *name, const char *value, int overwrite) {
  static int (*real)(const char*, const char*, int) = 0;
  if (!real) real = dlsym(RTLD_NEXT, "setenv");
  if (name && strstr(name, "GDK")) { fprintf(stderr, "[shim] setenv(%s=%s) blocked\n", name, value); return 0; }
  return real(name, value, overwrite);
}
int putenv(char *s) {
  static int (*real)(char*) = 0;
  if (!real) real = dlsym(RTLD_NEXT, "putenv");
  if (s && strstr(s, "GDK")) { fprintf(stderr, "[shim] putenv(%s) blocked\n", s); return 0; }
  return real(s);
}
