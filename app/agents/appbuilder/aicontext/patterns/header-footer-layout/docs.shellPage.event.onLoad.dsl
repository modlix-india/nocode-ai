FUNCTION onLoad
    LOGIC
        setStore1: UIEngine.SetStore(path = "Store.pageData._global.currentTheme", value = LocalStore.currentTheme ?? 0)
            output
                setStore1_Copy_1: UIEngine.SetStore(path = "Store.pageData._global.currentLanguage", value = LocalStore.currentDocLanguage ?? 0) AFTER Steps.setStore1.output
                    output
                        setStore5: UIEngine.SetStore(path = "Store.pageData._global.settingsCurrentTheme", value = `0`) AFTER Steps.setStore1_Copy_1.output
                            output
                                if5: System.If(condition = Store.pageData._global.details = undefined) AFTER Steps.setStore5.output
                                    true
                                        getDetails: docs.GetDetails() AFTER Steps.if5.true
                                            output
                                                setStore8: UIEngine.SetStore(path = "Store.pageData._global.details", value = Steps.getDetails.output.details)
                                                loadFooter: _.LoadFooter() AFTER Steps.getDetails.output
                                    output
                                        setStore: UIEngine.SetStore(path = "Store.pageData._global.details", value = Store.pageData._global.details) AFTER Steps.if5.output
                                            output
                                                if4: System.If(condition = Store.pageData._global.currentTheme >= Store.pageData._global.details.theme.length) AFTER Steps.setStore.output
                                                    true
                                                        setStore6: UIEngine.SetStore(path = "Store.pageData._global.currentTheme", value = Store.pageData._global.details.theme.length - 1) AFTER Steps.if4.true
                                                    output
                                                        forEachLoop: System.Loop.ForEachLoop(source = Store.pageData._global.details.languages) AFTER Steps.if4.output, Steps.if2.false
                                                            iteration
                                                                if1: System.If(condition = Steps.forEachLoop.iteration.each.default)
                                                                    true
                                                                        break: System.Loop.Break(stepName = "forEachLoop") AFTER Steps.if1.true
                                                                        setStore2: UIEngine.SetStore(path = "Store.pageData._global.currentLanguage", value = Steps.forEachLoop.iteration.index) AFTER Steps.if1.true
                                        if2: System.If(condition = LocalStore.currentDocLanguage != undefined) AFTER Steps.if5.output
                                            output
                                                if3: System.If(condition = Store.pageData._global.currentLanguage > Store.pageData._global.details.languages.length) AFTER Steps.if2.output
                                                    true
                                                        setStore4: UIEngine.SetStore(path = "Store.pageData._global.currentLanguage", value = `0`) AFTER Steps.if3.true
                                if6: System.If(condition = Store.pageData._global.menuData = undefined) AFTER Steps.setStore5.output
                                    true
                                        getMenu: docs.GetMenu() AFTER Steps.if6.true
                                            output
                                                setStore9: UIEngine.SetStore(path = "Store.pageData._global.menuData", value = Steps.getMenu.output.menu)
                                    output
                                        setStore7: UIEngine.SetStore(path = "Store.pageData._global.menuData", value = Store.pageData._global.menuData) AFTER Steps.if6.output
                                            output
                                                setCurrentMenuItem: _.SetCurrentMenuItem() AFTER Steps.setStore7.output