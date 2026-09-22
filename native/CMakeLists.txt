cmake_minimum_required(VERSION 3.16)
project(aura_redis_core C)
set(CMAKE_C_STANDARD 11)
add_library(aura_redis_core SHARED src/ar_core.c)
target_include_directories(aura_redis_core PUBLIC include)
set_target_properties(aura_redis_core PROPERTIES
  OUTPUT_NAME aura_redis_core
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}"
  POSITION_INDEPENDENT_CODE ON)
