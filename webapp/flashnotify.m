/*
 * flashnotify —— 原生 AppKit 闪屏提醒窗（替代 tkinter 子进程）
 *
 * 编译（在 webapp 目录）：
 *   clang -fobjc-arc -framework AppKit -o flashnotify flashnotify.m
 *
 * 用法：flashnotify <title> <msg> <url> <color#rrggbb> <dur秒> <slot 0-3>
 *
 * 2026-11-11：tkinter 版本在本机 macOS 上窗口内容不合成（只剩白色 "tk" 空窗），
 * 且每个 Python.app 子进程都会在 Dock 冒一个火箭图标。改成 AppKit 无边框窗后：
 * 内容 100% 由系统绘制、floating 层级置顶、Accessory 策略不占 Dock/菜单栏。
 */
#import <AppKit/AppKit.h>

static NSString *gTitle = @"";
static NSString *gLogPath = nil;

static void FlashLog(NSString *line) {
    NSDateFormatter *f = [[NSDateFormatter alloc] init];
    [f setDateFormat:@"HH:mm:ss"];
    NSString *s = [NSString stringWithFormat:@"%@ %@\n", [f stringFromDate:[NSDate date]], line];
    NSFileHandle *h = [NSFileHandle fileHandleForWritingAtPath:gLogPath];
    if (!h) {
        [[NSFileManager defaultManager] createFileAtPath:gLogPath contents:nil attributes:nil];
        h = [NSFileHandle fileHandleForWritingAtPath:gLogPath];
    }
    [h seekToEndOfFile];
    [h writeData:[s dataUsingEncoding:NSUTF8StringEncoding]];
    [h closeFile];
}

static NSColor *ColorFromHex(NSString *hex, NSColor *fallback) {
    if ([hex length] != 7 || ![hex hasPrefix:@"#"]) return fallback;
    unsigned int v = 0;
    NSScanner *sc = [NSScanner scannerWithString:[hex substringFromIndex:1]];
    if (![sc scanHexInt:&v]) return fallback;
    return [NSColor colorWithCalibratedRed:((v >> 16) & 255) / 255.0
                                     green:((v >> 8) & 255) / 255.0
                                      blue:(v & 255) / 255.0
                                     alpha:1.0];
}

@interface FlashView : NSView
@property (nonatomic, strong) NSWindow *win;
@property (nonatomic, strong) NSApplication *app;
@property (nonatomic, copy) NSString *url;
@property (nonatomic, copy) NSString *why;
@property (nonatomic, strong) NSColor *bg;
@end

@implementation FlashView
/* 2026-11-11 用户要求「恢复之前的」：严格复刻旧 tkinter 闪窗外观——
   直角纯色矩形、无圆角 / 无描边 / 无阴影，热帧主色、冷帧纯白。 */
- (void)drawRect:(NSRect)dirty {
    [self.bg setFill];
    NSRectFill(self.bounds);
}
/* 让整块窗口都接收点击（子 TextView 不抢事件） */
- (NSView *)hitTest:(NSPoint)point { return self; }
- (void)mouseDown:(NSEvent *)event {
    if (self.why) return;
    self.why = self.url.length ? @"click→关闭并打开Chrome" : @"click→关闭(无链接)";
    FlashLog([NSString stringWithFormat:@"%@ %@", self.why, gTitle]);
    if (self.url.length) {
        NSURL *u = [NSURL URLWithString:self.url];
        if (u) {
            BOOL ok = [[NSWorkspace sharedWorkspace] openURLs:@[u]
                                        withAppBundleIdentifier:@"com.google.Chrome"
                                                        options:NSWorkspaceLaunchDefault
                                   additionalEventParamDescriptor:nil
                                                launchIdentifiers:nil];
            if (!ok) [[NSWorkspace sharedWorkspace] openURL:u];
        }
    }
    [self.app terminate:nil];
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSArray *args = [[NSProcessInfo processInfo] arguments];
        NSString *title = args.count > 1 ? args[1] : @"";
        NSString *msg   = args.count > 2 ? args[2] : @"";
        NSString *url   = args.count > 3 ? args[3] : @"";
        NSString *hex   = args.count > 4 ? args[4] : @"#c02626";
        double dur      = args.count > 5 ? [args[5] doubleValue] : 12.0;
        int slot        = args.count > 6 ? [args[6] intValue] : 0;
        if (dur <= 0) dur = 12.0;
        gTitle = title;
        gLogPath = [NSHomeDirectory() stringByAppendingPathComponent:
                    @"WorkBuddy/Claw/webapp/tlbreak_flash.log"];

        NSColor *hotColor = ColorFromHex(hex, [NSColor colorWithCalibratedRed:0.75 green:0.15 blue:0.15 alpha:1]);
        NSFont *bold = [NSFont fontWithName:@"PingFangSC-Semibold" size:13] ?: [NSFont boldSystemFontOfSize:13];
        NSFont *regular = [NSFont fontWithName:@"PingFangSC-Regular" size:10] ?: [NSFont systemFontOfSize:10];
        NSFont *small = [NSFont fontWithName:@"PingFangSC-Regular" size:9] ?: [NSFont systemFontOfSize:9];

        /* 旧版规格：宽 326、高约 149（标题1行+正文2行+提示1行）；文本长时小幅增高，
           绝不再出现 10:47 那版 430pt 大窗 / 汇总窗 */
        NSInteger titleLines = MIN(2, MAX(1, (NSInteger)ceil((double)[title length] / 24.0)));
        NSInteger msgLines = MIN(7, MAX(1, (NSInteger)ceil((double)[msg length] / 34.0)));
        CGFloat w = 326;
        CGFloat h = MIN(210, 26 + titleLines * 19 + msgLines * 15 + 30);
        if (h < 150) h = 150;

        NSRect screen = [[NSScreen mainScreen] frame];
        CGFloat top = 48 + (slot % 4) * 132;
        NSRect frame = NSMakeRect(NSMaxX(screen) - w - 16,
                                  NSMaxY(screen) - top - h, w, h);

        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];

        NSWindow *win = [[NSWindow alloc] initWithContentRect:frame
                                                   styleMask:NSWindowStyleMaskBorderless
                                                     backing:NSBackingStoreBuffered
                                                       defer:NO];
        [win setLevel:NSFloatingWindowLevel];
        [win setOpaque:YES];
        [win setBackgroundColor:hotColor];
        [win setHasShadow:NO];
        [win setHidesOnDeactivate:NO];
        [win setCanHide:NO];
        [win setCollectionBehavior:NSWindowCollectionBehaviorCanJoinAllSpaces |
                                   NSWindowCollectionBehaviorStationary |
                                   NSWindowCollectionBehaviorIgnoresCycle];

        FlashView *view = [[FlashView alloc] initWithFrame:NSMakeRect(0, 0, w, h)];
        view.win = win; view.app = app; view.url = url;
        [win setContentView:view];

        NSTextView *tv = [[NSTextView alloc] initWithFrame:NSMakeRect(14, 14, w - 28, h - 28)];
        [tv setEditable:NO]; [tv setSelectable:NO]; [tv setDrawsBackground:NO];
        [[tv textContainer] setLineFragmentPadding:0];
        [tv setTextContainerInset:NSMakeSize(0, 0)];
        [view addSubview:tv];

        __block BOOL hot = YES;
        void (^paint)(void) = ^{
            /* 与旧 FLASH_SRC 逐项对齐：热帧 #c02626 / 冷帧纯白；
               标题 白|#1a1a1a、正文 #ffe08a|#333333、提示 白|#1d4ed8 */
            NSColor *bg = hot ? hotColor : [NSColor whiteColor];
            NSColor *c1 = hot ? [NSColor whiteColor] : [NSColor colorWithCalibratedRed:0.10 green:0.10 blue:0.10 alpha:1];
            NSColor *c2 = hot ? [NSColor colorWithCalibratedRed:1 green:0.88 blue:0.54 alpha:1]
                              : [NSColor colorWithCalibratedRed:0.20 green:0.20 blue:0.20 alpha:1];
            NSColor *c3 = hot ? [NSColor whiteColor] : [NSColor colorWithCalibratedRed:0.11 green:0.31 blue:0.85 alpha:1];
            view.bg = bg; [view setNeedsDisplay:YES];
            [view.win setBackgroundColor:bg];
            NSMutableAttributedString *s = [[NSMutableAttributedString alloc] init];
            [s appendAttributedString:[[NSAttributedString alloc]
                initWithString:[title stringByAppendingString:@"\n"]
                    attributes:@{NSFontAttributeName: bold, NSForegroundColorAttributeName: c1}]];
            [s appendAttributedString:[[NSAttributedString alloc]
                initWithString:[msg stringByAppendingString:@"\n"]
                    attributes:@{NSFontAttributeName: regular, NSForegroundColorAttributeName: c2}]];
            [s appendAttributedString:[[NSAttributedString alloc]
                initWithString:@"👉 点击在 Chrome 查看"
                    attributes:@{NSFontAttributeName: small, NSForegroundColorAttributeName: c3}]];
            [[tv textStorage] setAttributedString:s];
        };
        paint();
        [win makeKeyAndOrderFront:nil];
        FlashLog([NSString stringWithFormat:@"start %@", gTitle]);

        [NSTimer scheduledTimerWithTimeInterval:0.4 repeats:YES block:^(NSTimer *t) {
            hot = !hot;
            paint();
        }];

        __weak FlashView *weakView = view;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(dur * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
            FlashView *v = weakView;
            if (v && !v.why) {
                FlashLog([NSString stringWithFormat:@"timeout→%d秒自动关 %@", (int)dur, gTitle]);
                [app terminate:nil];
            }
        });
        [app run];
    }
    return 0;
}
