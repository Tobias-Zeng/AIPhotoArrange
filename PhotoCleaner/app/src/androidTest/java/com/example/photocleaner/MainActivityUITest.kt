package com.example.photocleaner

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * UI 仪器化测试
 * 
 * 测试内容：
 * 1. 主界面三步流程显示
 * 2. 按钮点击交互
 * 3. 状态切换
 */
@RunWith(AndroidJUnit4::class)
class MainActivityUITest {
    
    @get:Rule
    val composeTestRule = createAndroidComposeRule<MainActivity>()
    
    /**
     * 测试：初始界面显示导入步骤
     */
    @Test
    fun mainScreen_initialState_showsImportStep() {
        // 验证标题存在
        composeTestRule.onNodeWithText("请导入删除列表文件").assertExists()
        
        // 验证按钮存在
        composeTestRule.onNodeWithText("选择文件").assertExists()
            .assertIsDisplayed()
            .assertHasClickAction()
    }
    
    /**
     * 测试：导入步骤提示文本显示
     */
    @Test
    fun importStep_showsInstructionText() {
        composeTestRule.onNodeWithText("从电脑生成的 non_highlight_photos_*.txt 文件中导入")
            .assertExists()
            .assertIsDisplayed()
    }
    
    /**
     * 测试：完成步骤显示（模拟场景）
     * 注意：此测试需要 mock 数据，实际实现需配合依赖注入
     */
    @Test
    fun completedStep_showsSuccessMessage() {
        // 此测试演示验证逻辑，实际需要 mock PhotoMatcher 和 PhotoDeleter
        // 或通过 UI 流程完整走通（需要真实文件和权限）
        
        // 预期验证内容示例：
        // composeTestRule.onNodeWithText("已成功删除").assertExists()
        // composeTestRule.onNodeWithText("重新导入").assertExists()
    }
}
