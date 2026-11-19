/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { BasicAPIs } from './api';
import { generateAPIs } from './base';

const Basic = generateAPIs(BasicAPIs);

export default {
  Basic,
};
