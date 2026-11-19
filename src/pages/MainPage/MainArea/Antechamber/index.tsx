/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useDispatch } from 'react-redux';
import { Link } from 'react-router-dom';
import { isMobile } from '@/utils/utils';
import InvokeButton from '@/pages/MainPage/MainArea/Antechamber/InvokeButton';
import { useJoin, useScene } from '@/lib/useCommon';
import AIChangeCard from '@/components/AiChangeCard';
import { updateFullScreen, updateShowSubtitle } from '@/store/slices/room';
import style from './index.module.less';

function Antechamber() {
  const dispatch = useDispatch();
  const [joining, dispatchJoin] = useJoin();
  const { isScreenMode, isAvatarScene, runtimeMode, rtcAvailable } = useScene();

  const handleJoinRoom = () => {
    dispatch(updateFullScreen({ isFullScreen: !isMobile() && !isScreenMode && !isAvatarScene })); // 初始化
    dispatch(updateShowSubtitle({ isShowSubtitle: !isAvatarScene }));

    if (!joining) {

      // 开启RTC服务，你进来，Ai进来，开始通话
      dispatchJoin();
    }
  };

  return (
    <div className={`${style.wrapper} ${isMobile() ? style.mobile : ''}`}>
      <AIChangeCard />
      {runtimeMode === 'agent_loop' && !rtcAvailable ? <p role="status">语音暂不可用，<Link to="/chat">开始文字对话</Link></p> :
        <InvokeButton onClick={handleJoinRoom} loading={joining} className={style['invoke-btn']} />}
      {isMobile() ? null : (
        <div className={style.description}>支持文字与实时语音对话</div>
      )}
    </div>
  );
}

export default Antechamber;
