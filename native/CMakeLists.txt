cmake_minimum_required(VERSION 3.16)
project(aura_redis_core C)
set(CMAKE_C_STANDARD 11)
add_library(aura_redis_core SHARED
  src/ar_core.c
  src/ar_server.c)
target_include_directories(aura_redis_core PUBLIC include)
target_include_directories(aura_redis_core PRIVATE src)
set_target_properties(aura_redis_core PROPERTIES
  OUTPUT_NAME aura_redis_core
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
  POSITION_INDEPENDENT_CODE ON
  C_VISIBILITY_PRESET default)
target_compile_options(aura_redis_core PRIVATE -O2 -Wall -Wextra)

add_executable(aura_redis_server src/ar_main.c)
target_link_libraries(aura_redis_server PRIVATE aura_redis_core)
target_include_directories(aura_redis_server PRIVATE include)
set_target_properties(aura_redis_server PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")
target_compile_options(aura_redis_server PRIVATE -O2 -Wall -Wextra)
