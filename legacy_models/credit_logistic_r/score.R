#!/usr/bin/env Rscript
args<-commandArgs(trailingOnly=TRUE)
arg<-function(flag,default=NULL){i<-match(flag,args);if(!is.na(i)&&i<length(args))args[i+1] else default}
input<-arg('--input');output<-arg('--output');package<-arg('--package',getwd())
if(is.null(input)||is.null(output))stop('Usage: Rscript score.R --input INPUT.csv --output OUTPUT.csv')
artifact<-file.path(package,'model.rds')
if(!file.exists(artifact))stop('Trained artifact missing')
if(!requireNamespace('digest',quietly=TRUE)||!requireNamespace('jsonlite',quietly=TRUE))stop('Missing scoring dependencies')
manifest<-jsonlite::fromJSON(file.path(package,'manifest.json'))
if(!identical(digest::digest(file=artifact,algo='sha256'),manifest$artifact_sha256))stop('Artifact hash mismatch')
m<-readRDS(artifact) # Load only trusted, checksum-verified RDS in isolated worker.
# Both historic full glm.fit and new coefficients-only packages expose this field.
if(!is.list(m$fit)||!is.numeric(m$fit$coefficients)||
   length(m$fit$coefficients)!=length(m$features)+1L||
   any(!is.finite(m$fit$coefficients))||
   length(m$center)!=length(m$features)||
   length(m$scale)!=length(m$features)||
   any(!is.finite(m$center))||any(!is.finite(m$scale))||any(m$scale==0)) {
  stop('Invalid R GLM coefficients or preprocessing metadata')
}
df<-read.csv(input,check.names=FALSE,colClasses='character',fileEncoding='UTF-8')
if(!'row_id' %in% names(df)||nrow(df)==0||anyNA(df$row_id)||any(!nzchar(trimws(df$row_id)))||anyDuplicated(df$row_id))stop('Invalid row_id')
if(any(c('Class','default payment next month') %in% names(df)))stop('Target forbidden')
if(!all(m$features %in% names(df)))stop('Missing required features')
X<-sapply(df[,m$features,drop=FALSE],function(x)suppressWarnings(as.numeric(x)))
if(!is.matrix(X))X<-matrix(X,ncol=length(m$features))
if(!all(is.finite(X)))stop('Invalid numeric feature')
z<-sweep(sweep(X,2,m$center,'-'),2,m$scale,'/')
p<-as.numeric(plogis(cbind(1,z)%*%m$fit$coefficients))
if(!all(is.finite(p))||any(p<0|p>1))stop('Probability outside [0,1]')
write.csv(data.frame(row_id=df$row_id,positive_probability=p),output,row.names=FALSE,quote=TRUE)
