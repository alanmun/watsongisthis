import redis
import json

#https://stackoverflow.com/questions/19581059/misconf-redis-is-configured-to-save-rdb-snapshots

#rdb is a static class tasked with handling whatsong's redis db operations
class rdb:
    r = redis.Redis() #Use default localhost:6379, default db #0, no pw

    @staticmethod
    def getNumberOfUsersAndJobs():
        usrs = rdb.getUsers()
        totalJobs = 0
        for [_, jobs] in usrs.items():
            totalJobs += len(jobs)
        return (len(usrs.keys()), totalJobs)

    @staticmethod
    def getUsers():
        usrs = None
        try:
            usrs = json.loads(rdb.r.get("users"))
        except:
            return {} #No users exists
        return usrs

    @staticmethod #Raw jobs are simply jobs completed that are only tracking the asker's ID
    def getRawJobs():
        return json.loads(rdb.r.get("jobs"))

    @staticmethod
    def writeJob(username, askerTweetID, generatedURL=None):
        # users: {
        #     UNKNOWN: {
        #         jobs: [
        #             job1, job2, etc
        #         ]
        #     }
        # }
        serializedJobData = json.dumps({
            "tweetID": askerTweetID,
            "generatedURL": generatedURL
        })
        usrs = rdb.getUsers()
        rawJobs = rdb.getRawJobs()
        rawJobs.append(askerTweetID)
        if username in usrs: usrs[username].append(serializedJobData)
        else:                usrs[username] = [serializedJobData]

        rdb.r.set("jobs", json.dumps(rawJobs))
        rdb.r.set("users", json.dumps(usrs))

    @staticmethod
    def writeLegacyJobs(usrs, jobs, username, askerTweetID, generatedURL=None):
        serializedJobData = json.dumps({
            "tweetID": askerTweetID,
            "generatedURL": generatedURL
        })

        jobs.append(askerTweetID)
        if username in usrs: usrs[username].append(serializedJobData)
        else:                usrs[username] = [serializedJobData]
        return [usrs, jobs]

if __name__ == "__main__":
    rdb.r = redis.Redis()
    print("Number of users, jobs in db: ", rdb.getNumberOfUsersAndJobs())
    #print(rdb.getRawJobs())

    #To load in legacy jobs:
    usrs = rdb.getUsers()
    jobs = []
    for l in open("happycustomers.txt", "r").readlines():
        usrs, jobs = rdb.writeLegacyJobs(usrs, jobs, "UNKNOWN", l, "NOT_AVAILABLE")
    rdb.r.set("users", json.dumps(usrs))
    rdb.r.set("jobs", json.dumps(jobs))
