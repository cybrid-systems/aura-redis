cmake_minimum_required(VERSION 3.16)
project(aura_redis_core C)
set(CMAKE_C_STANDARD 11)

# P2.15 — optional native OpenSSL TLS (soft: build works without it)
option(AURA_REDIS_TLS "Enable native OpenSSL TLS (--tls-port)" ON)
set(AURA_REDIS_HAS_TLS 0)
if(AURA_REDIS_TLS)
  find_package(OpenSSL)
  if(OpenSSL_FOUND)
    set(AURA_REDIS_HAS_TLS 1)
    message(STATUS "aura-redis: OpenSSL TLS enabled (${OPENSSL_VERSION})")
  else()
    message(STATUS "aura-redis: OpenSSL not found — TLS disabled (cleartext only)")
  endif()
endif()

add_library(aura_redis_core SHARED
  src/ar_core.c
  src/ar_types.c
  src/ar_server.c
  src/ar_rdb.c
  src/ar_tls.c)
target_include_directories(aura_redis_core PUBLIC include)
target_include_directories(aura_redis_core PRIVATE src)
set_target_properties(aura_redis_core PROPERTIES
  OUTPUT_NAME aura_redis_core
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
  POSITION_INDEPENDENT_CODE ON
  C_VISIBILITY_PRESET default)
target_compile_options(aura_redis_core PRIVATE -O2 -Wall -Wextra)
target_link_libraries(aura_redis_core PRIVATE dl)
if(AURA_REDIS_HAS_TLS)
  target_compile_definitions(aura_redis_core PRIVATE AURA_REDIS_HAS_TLS=1)
  target_link_libraries(aura_redis_core PRIVATE OpenSSL::SSL OpenSSL::Crypto)
endif()

add_executable(aura_redis_server src/ar_main.c)
target_link_libraries(aura_redis_server PRIVATE aura_redis_core)
target_include_directories(aura_redis_server PRIVATE include)
set_target_properties(aura_redis_server PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
target_compile_options(aura_redis_server PRIVATE -O2 -Wall -Wextra)

# Iteration 7 — sample eviction plugin (dlopen)
add_library(ar_evict_random SHARED plugins/evict_random.c)
target_include_directories(ar_evict_random PRIVATE include)
target_link_libraries(ar_evict_random PRIVATE aura_redis_core)
set_target_properties(ar_evict_random PROPERTIES
  OUTPUT_NAME ar_evict_random
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
  POSITION_INDEPENDENT_CODE ON)
target_compile_options(ar_evict_random PRIVATE -O2 -Wall -Wextra)

# Iteration 7 stretch — second sample plugin (live-reload swap target)
add_library(ar_evict_rr SHARED plugins/evict_rr.c)
target_include_directories(ar_evict_rr PRIVATE include)
target_link_libraries(ar_evict_rr PRIVATE aura_redis_core)
set_target_properties(ar_evict_rr PROPERTIES
  OUTPUT_NAME ar_evict_rr
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/plugins"
  POSITION_INDEPENDENT_CODE ON)
target_compile_options(ar_evict_rr PRIVATE -O2 -Wall -Wextra)
