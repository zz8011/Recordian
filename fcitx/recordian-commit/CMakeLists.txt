cmake_minimum_required(VERSION 3.16)
project(recordian-commit CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Fcitx5Core REQUIRED)

include(GNUInstallDirs)
# Ubuntu fcitx5 cmake packages do not export FCITX_INSTALL_*; use the
# standard Debian addon locations, overridable on the command line.
if(NOT DEFINED FCITX_INSTALL_ADDONDIR)
    set(FCITX_INSTALL_ADDONDIR "${CMAKE_INSTALL_FULL_LIBDIR}/fcitx5")
endif()
if(NOT DEFINED FCITX_INSTALL_ADDONDESCDIR)
    set(FCITX_INSTALL_ADDONDESCDIR "${CMAKE_INSTALL_FULL_DATAROOTDIR}/fcitx5/addon")
endif()

add_library(recordian-commit MODULE recordian-commit.cpp)
target_link_libraries(recordian-commit Fcitx5::Core)
set_target_properties(recordian-commit PROPERTIES
    PREFIX "lib"
    OUTPUT_NAME "recordian-commit")

install(TARGETS recordian-commit DESTINATION "${FCITX_INSTALL_ADDONDIR}")
install(FILES recordian-commit.conf DESTINATION "${FCITX_INSTALL_ADDONDESCDIR}")
