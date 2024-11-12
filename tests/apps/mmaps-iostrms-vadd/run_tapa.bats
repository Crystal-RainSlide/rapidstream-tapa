# Copyright (c) 2024 RapidStream Design Automation, Inc. and contributors.
# All rights reserved. The contributor(s) of this file has/have agreed to the
# RapidStream Contributor License Agreement.

compile_host() {
  cd "${BATS_TEST_DIRNAME}"

  tapa g++ \
    vadd-host.cpp vadd.cpp \
    -o ${BATS_TMPDIR}/mmaps-iostrms-vadd-host

  [ -x "${BATS_TMPDIR}/mmaps-iostrms-vadd-host" ]
}

compile_xo() {
  cd "${BATS_TEST_DIRNAME}"

  ${RAPIDSTREAM_TAPA_HOME}/usr/bin/tapa \
    -w ${BATS_TMPDIR}/mmaps-iostrms-vadd-workdir \
    compile \
    --jobs 1 \
    --platform xilinx_u250_gen3x16_xdma_4_1_202210_1 \
    -f vadd.cpp \
    -t VecAdd \
    -o ${BATS_TMPDIR}/mmaps-iostrms-vadd.xo

  [ -f "${BATS_TMPDIR}/mmaps-iostrms-vadd.xo" ]
}

@test "apps/vavadd-mmaps-strmsdd: tapa c simulation passes" {
  compile_host
  ${BATS_TMPDIR}/mmaps-iostrms-vadd-host
}

@test "apps/mmaps-iostrms-vadd: tapa generates an xo file and its simulation passes" {
  compile_xo
  ${BATS_TMPDIR}/mmaps-iostrms-vadd-host --bitstream ${BATS_TMPDIR}/mmaps-iostrms-vadd.xo 1000
}
