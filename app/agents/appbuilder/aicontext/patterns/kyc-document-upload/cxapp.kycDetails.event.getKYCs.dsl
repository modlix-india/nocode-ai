FUNCTION getKYCs
    LOGIC
        if1: System.If(condition = Page.userId != undefined)
            true
                setStore3: UIEngine.SetStore(path = "Page.jointArray", value = []) AFTER Steps.if1.true
                    output
                        getKycsById: kyc.getKycsById(userId = Page.userId) AFTER Steps.setStore3.output
                            output
                                if3: System.If(condition = Steps.getKycsById.output.kycDetails.length != 0)
                                    true
                                        setStore1: UIEngine.SetStore(path = "Page.kycUsers", value = Steps.getKycsById.output.kycDetails) AFTER Steps.if3.true
                                            output
                                                setStore2: UIEngine.SetStore(path = "Page.verifiedUsers", value = []) AFTER Steps.setStore1.output
                                                    output
                                                        forEachLoop: System.Loop.ForEachLoop(source = Page.kycUsers) AFTER Steps.setStore2.output
                                                            iteration
                                                                if: System.If(condition = `Steps.forEachLoop.iteration.each.status = 'VERIFIED'`)
                                                                    true
                                                                        if4: System.If(condition = Steps.forEachLoop.iteration.each.joint) AFTER Steps.if.true
                                                                            true
                                                                                jointIdsToObjects: cxapp.jointIdsToObjects(kycId = Steps.forEachLoop.iteration.each._id) AFTER Steps.if4.true
                                                                                    output
                                                                                        setStore9: UIEngine.SetStore(path = "Page.fetchedJointKyc", value = Steps.jointIdsToObjects.output.kyc)
                                                                                            output
                                                                                                insertLast11: System.Array.InsertLast(element = Page.fetchedJointKyc, source = Page.verifiedUsers) AFTER Steps.setStore9.output
                                                                                                    output
                                                                                                        setStore_Copy_1: UIEngine.SetStore(path = "Page.verifiedUsers", value = Steps.insertLast11.output.result)
                                                                            false
                                                                                insertLast: System.Array.InsertLast(element = Steps.forEachLoop.iteration.each, source = Page.verifiedUsers) AFTER Steps.if4.false
                                                                                    output
                                                                                        setStore: UIEngine.SetStore(path = "Page.verifiedUsers", value = Steps.insertLast.output.result) AFTER Steps.insertLast.output
                                                            output
                                                                forEachLoop1: System.Loop.ForEachLoop(source = Page.verifiedUsers) AFTER Steps.forEachLoop.output
                                                                    iteration
                                                                        if2: System.If(condition = Steps.forEachLoop1.iteration.each.joint!=undefined)
                                                                            true
                                                                                objectKeys: System.Object.ObjectKeys(source = Steps.forEachLoop1.iteration.each.joint) AFTER Steps.if2.true
                                                                                    output
                                                                                        setStore4: UIEngine.SetStore(path = "Page.jointArray", value = Steps.objectKeys.output.value)
                                                                                            output
                                                                                                setStore5: UIEngine.SetStore(path = `'Page.verifiedUsers[{{Steps.forEachLoop1.iteration.index}}].joint.length'`, value = Page.jointArray.length) AFTER Steps.setStore4.output
                                                                                                    output
                                                                                                        setStore6: UIEngine.SetStore(path = `'Page.verifiedUsers[{{Steps.forEachLoop1.iteration.index}}].joint.jointSelect'`, value = "Joint account") AFTER Steps.setStore5.output
                                                                                                            output
                                                                                                                setStore7: UIEngine.SetStore(path = `'Page.verifiedUsers[{{Steps.forEachLoop1.iteration.index}}].joint.visibility'`, value = false) AFTER Steps.setStore6.output
                                    false
                                        setStore8: UIEngine.SetStore(path = "Page.zeroKycs", value = `true`) AFTER Steps.if3.false