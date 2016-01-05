from pyvcsshark.datastores.basestore import BaseStore
from pyvcsshark.dbmodels.mongomodels import *
from datetime import datetime
from mongoengine import connect

import multiprocessing
import logging
import os


class MongoStore(BaseStore):
    """ Datastore implementation for saving data to the mongodb. Inherits from :class:`pyvcsshark.datastores.basestore.BaseStore`.
    
    :property commitqueue: instance of a :class:`multiprocessing.JoinableQueue`, which  \
    holds objects of :class:`pyvcsshark.dbmodels.models.CommitModel`, that should be put into the mongodb
    :property NUMBER_OF_PROCESSES: holds the number of processes by calling :func:`multiprocessing.cpu_count`
    :property logger: holds the logging instance, by calling logging.getLogger("store")
    """
    
    commitqueue = None
    #NUMBER_OF_PROCESSES = 1
    NUMBER_OF_PROCESSES = multiprocessing.cpu_count()
    logger = logging.getLogger("store")
    
    def __init__(self):
        BaseStore.__init__(self)
        
    def initialize(self, dbname, host, port, user , password, projectname, repositoryURL, type=None):
        """Initializes the mongostore by connecting to the mongodb, creating the project in the project collection \
        and setting up processes (see: :class:`pyvcsshark.datastores.mongostore.CommitStorageProcess`, which
        read commits out of the commitqueue, process them and store them into the mongodb.
        
        :param dbname: name of the mongo database to use
        :param host: host where the mongodb runs on
        :param port: port where the mongodb server is listening on
        :param user: user used for authentication
        :param password: password for the authentication
        :param projectname: name of the project of the repository which is parsed
        :param repositoryURL: url of the repository which is parsed
        :param type: type of the repository which is parsed (e.g. git)
        """
        
        self.repositoryURL = repositoryURL
        
        self.logger.info("Initializing MongoStore...")
        # Create queue for multiprocessing
        self.commitqueue = multiprocessing.JoinableQueue()
        # We define, that the user we authenticate with is in the admin database
        self.logger.info("Connecting to MongoDB...")
        connect(dbname, username=user, password=password, host=host, port=port, authentication_source='admin', connect=False)

        # Update project if project with the same url is already in the mongodb and add if not
        project = Project.objects(url=repositoryURL).upsert_one(url=repositoryURL, repositoryType=type, name=projectname)
        
        # Get the last commit by date of the project (if there is any)
        lastCommitDate = Commit.objects(projectId=project.id).only('committerDate').order_by('-committerDate').first()
        
        if(lastCommitDate is not None):
            lastCommitDate = lastCommitDate.committerDate


        # Start worker, they will wait till something comes into the queue and then process it
        for i in range(self.NUMBER_OF_PROCESSES):
            process = CommitStorageProcess(self.commitqueue, project.id, lastCommitDate)
            process.daemon=True
            process.start()

        self.logger.info("Starting storage Process...")
        

    
    @property
    def storeIdentifier(self):
        """Returns the identifier **mongo** for this datastore"""
        return 'mongo'


    def addCommit(self, commitModel):
        """Adds commits of class :class:`pyvcsshark.dbmodels.models.CommitModel` to the commitqueue"""
        # add to queue
        self.commitqueue.put(commitModel)
        return

    def deleteAll(self):
        """Deletes all data of one project from the datastore
        
        .. WARNING:: Data from the people collection is not deleted, as these documents may be used by other projects!"""
        
        # Get project id
        projectId = Project.objects(url=self.repositoryURL).only("id").first().id
        
        # delete tags
        Tag.objects(projectId=projectId).delete()
        
        
        # delete commits
        Commit.objects(projectId=projectId).delete()
        
        
        # delete file actions and hunks
        fileActions = FileAction.objects(projectId=projectId).only("hunkIds")
        
        for fileAction in fileActions:
            for hunkId in fileAction.hunkIds:
                Hunk.objects(id=hunkId).delete()
            fileAction.delete()
        
        # delete files
        File.objects(projectId=projectId).delete()
        
        # delete project
        Project.objects(id=projectId).delete()
        
        
        
        return
    
    def finalize(self):
        """Wait till all commits are processed, by calling a join on the queue"""
        self.commitqueue.join()
        self.logger.info("Storing Process complete...")
        return
    

class CommitStorageProcess(multiprocessing.Process):
    """Class that inherits from :class:`multiprocessing.Process` for processing instances of class :class:`pyvcsshark.dbmodels.models.CommitModel` \
    and writing it into the mongodb
    
    :param queue: queue, where the :class:`pyvcsshark.dbmodels.models.CommitModel` are stored in
    :param projectId: object id of class :class:`bson.objectid.ObjectId` from the project 
    :param lastCommitDate: object of class :class:`datetime.datetime`, which holds the last commit that was parsed 
    """
    def __init__(self, queue, projectId, lastCommitDate):
        multiprocessing.Process.__init__(self)
        self.queue = queue
        self.projectId = projectId
        self.lastCommitDate = lastCommitDate
        
    def run(self):   
        """ Endless loop for the processes, which consists of several steps:
        
        1. Get a object of class :class:`pyvcsshark.dbmodels.models.CommitModel` from the queue
        2. Check if this commit was stored before and if it is so: update branches and tags (if they have changed)
        3. Store author and committer in mongodb
        4. Store Tags in mongodb
        5. Create a list of branches, where the commit belongs to
        6. Save the different file actions, which were done in this commit in the mongodb
        7. Save the commit itself
        
        
        .. NOTE:: The committer date is used to check if a commit was already stored before. Meaning: We get the \
        last commit out of the database and check if the committer date of the commits we process are > than the \
        committer date of the last commit.
        
        .. WARNING:: We only look for changed tags and branches here for already processed commits!
        """
        while True:
            commit = self.queue.get()

            # Check if commitdate > lastcommit date
            if(self.lastCommitDate is not None and commit.committerDate <= self.lastCommitDate):
                # We have parsed that commit before, now we need to check if branches or tags were changed
                self.checkAndUpdateBranchesAndTags(commit)
                
                # Nothing more than branches or tags can be changed, therefore we only need to update the commit here
                self.queue.task_done()
                continue

            # Create people 
            authorId = self.createPeople(commit.author.name, commit.author.email)
            committerId = self.createPeople(commit.committer.name, commit.committer.email)
            
            # Create tag list
            tagIds = self.createTagList(commit.tags)

            # Create branchlist
            branches = self.createBranchList(commit.branches)
            
            
            # Create fileActions
            fileActionIds = self.createFileActions(commit.changedFiles, commit.id)
            
            
            # Create Revision object#
            mongoCommit = Commit(projectId = self.projectId,
                                 revisionHash = commit.id,
                                 branches = branches,
                                 tagIds = tagIds ,
                                 parents=commit.parents,
                                 authorId=authorId,
                                 authorDate=commit.authorDate,
                                 authorOffset=commit.authorOffset,
                                 committerId=committerId,
                                 committerDate=commit.committerDate,
                                 committerOffset=commit.committerOffset,
                                 message=commit.message,
                                 fileActionIds= fileActionIds).save()      

            
            self.queue.task_done()

    def checkAndUpdateBranchesAndTags(self, commit):
        """ Method that checks if the commit that was stored in the database has the same
        branches and tags as the commit which is processed at the moment. 
        
        :param commit: object of class :class:`pyvcsshark.dbmodels.models.CommitModel`.
        
        .. NOTE:: We use the project id and the revision hash to find the commit in the datastore.
        """
        oldCommit = Commit.objects(projectId=self.projectId, revisionHash=commit.id).first()
        
        oldTagList = set(oldCommit.tagIds)
        newTagList = set(self.createTagList(commit.tags))
        
        oldBranchList = set(oldCommit.branches)
        newBranchList = set(self.createBranchList(commit.branches))
        
        # If they are not equal, we need to update the commit and delete old tags
        if(oldTagList != newTagList):
            tagsToDelete = list(oldTagList - newTagList)
            for tag in tagsToDelete:
                Tag.objects(id=tag).delete()
            Commit.objects(projectId=self.projectId, revisionHash=commit.id).update_one(tagIds = newTagList)
            
        # If they are not equal we need to update the commit
        if(oldBranchList != newBranchList):
            Commit.objects(projectId=self.projectId, revisionHash=commit.id).update_one(branches=newBranchList)
            
                    

    def createBranchList(self, branches):
        """Creates a list of the different branch names, where a commit belongs to. We go through the \
        branches property of the class :class:`pyvcsshark.dbmodels.models.CommitModel`, which is a list of \
        different branch objects of class `pyvcsshark.dbmodels.models.CommitModel`
        
        :param branches: list of objects of class :class:`pyvcsshark.dbmodels.models.BranchModel`
        """
        branchList = []
        for branch in branches:
            branchList.append(branch.name)
            
        return branchList
        
    def createTagList(self, tags):
        """ Creates a list of object Ids of the type :class:`bson.objectid.ObjectId` for the different tags.
        First it goes through the tag list of the commit of type :class:`pyvcsshark.dbmodels.models.CommitModel`, which is
        a list of different tag objects of class :class:`pyvcsshark.dbmodels.models.TagModel`. It transforms the :class:`pyvcsshark.dbmodels.models.TagModel`
        tags to tags of type :class:`pyvcsshark.dbmodels.mongomodels.Tag` to store it in the mongodb.
        
        .. NOTE:: If a tag is found, which belongs to the same project and has the same name it is overwritten (normally, this should not be possible)
        
        .. NOTE:: If the person who tagged the commit is NOT in the mongodb, it is created
        
        .. NOTE:: The call to :func:`mongoengine.queryset.QuerySet.upsert_one` is thread/process safe"""
        tagList = []
        for tag in tags:
            if tag.tagger is not None:
                taggerId = self.createPeople(tag.tagger.name, tag.tagger.email)
                mongoTag = Tag.objects(projectId = self.projectId, name = tag.name).upsert_one(projectId = self.projectId, name=tag.name, message=tag.message, 
                                                                                               taggerId=taggerId, date=tag.taggerDate, offset = tag.taggerOffset)
            else:
                mongoTag = Tag.objects(projectId = self.projectId, name = tag.name).upsert_one(projectId = self.projectId, name= tag.name, date=tag.taggerDate, offset=tag.taggerOffset)
                
            tagList.append(mongoTag.id)
        return tagList
    
    def createPeople(self, name, email):
        """ Creates a people object of type :class:`pyvcsshark.dbmodels.mongomodels.People` and returns a
        object id of the type :class:`bson.objectid.ObjectId` of the stored object
        
        :param name: name of the contributer
        :param email: email of the contributer
        
        .. NOTE:: The call to :func:`mongoengine.queryset.QuerySet.upsert_one` is thread/process safe
        """
        mongoPeople = People.objects(name=name, email=email).upsert_one(name=name, email=email)
        return mongoPeople.id
    
    def createFileActions(self, files, revisionHash):
        """ Creates a list of object ids of type :class:`bson.objectid.ObjectId` for the different file actions of the commit by
        transforming the files into file actions of type :class:`pyvcsshark.dbmodels.mongomodels.FileAction`, :class:`pyvcsshark.dbmodels.mongomodels.File`, and
        :class:`pyvcsshark.dbmodels.mongomodels.Hunk`
        
        :param files: list of changed files of type :class:`pyvcsshark.dbmodels.models.FileModel`
        :param revisionHash: revisionhash of the commit which is processed
        
        .. NOTE:: The call to :func:`mongoengine.queryset.QuerySet.upsert_one` is thread/process safe
        
        .. NOTE:: Hunks (type :class:`pyvcsshark.dbmodels.mongomodels.Hunk`)  and the file action itself are inserted via bulk insert.
        """
        
        fileActionList = []
        for file in files:
            
            # Check if the file was a copy or move action (then the oldPath attribute is not None)
            oldFileId = None
            if file.oldPath is not None:
                oldFile = File.objects(projectId=self.projectId, path=file.oldPath, name=os.path.basename(file.oldPath)).upsert_one(projectId=self.projectId, 
                                                                                                                                    path=file.oldPath,
                                                                                                                                    name=os.path.basename(file.oldPath))
                oldFileId = oldFile.id
                
            # Create hunk objects for bulk insert
            hunks = []
            for hunk in file.hunks:
                mongoHunk = Hunk(content=hunk)
                hunks.append(mongoHunk)
            
            # Get hunk ids from insert if hunks is not empty
            hunkIds = [] 
            if(hunks):
                hunkIds = Hunk.objects.insert(hunks, load_bulk=False)
    
            # Create a new file object
            newFile = File.objects(projectId=self.projectId, path=file.path, name=os.path.basename(file.path)).upsert_one(projectId=self.projectId, 
                                                                                                                          path=file.path,
                                                                                                                          name=os.path.basename(file.path))
            
            # Create the new file action and append it to the file action list for bulk insert
            fileAction = FileAction(projectId=self.projectId,
                                    fileId=newFile.id,
                                    revisionHash=revisionHash,
                                    sizeAtCommit=file.size,
                                    linesAdded = file.linesAdded,
                                    linesDeleted = file.linesDeleted,
                                    isBinary = file.isBinary,
                                    mode = file.mode,
                                    hunkIds = hunkIds,
                                    oldFilePathId=oldFileId) 
            fileActionList.append(fileAction)
            
        # Bulk insert all action ids
        fileActionIds = [] 
        if(fileActionList):
            fileActionIds = FileAction.objects.insert(fileActionList, load_bulk=False)   
        return fileActionIds 

    
    
    
    
    
        