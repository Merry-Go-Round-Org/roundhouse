#!/usr/bin/env Rscript
# R GLM; identical row_id and train-only split as Python, but independently fit.
args <- commandArgs(trailingOnly=TRUE)
getarg <- function(flag,default=NULL) {
  i<-match(flag,args);if(!is.na(i)&&i<length(args)) args[i+1] else default
}
domain<-getarg('--domain');package<-getarg('--package',getwd())
path<-getarg('--data',if(domain=='uci') 'datasets/uci/default of credit card clients.xls' else 'datasets/ulb/creditcard.csv')
splits_path<-getarg('--splits',paste0('legacy_models/evidence/',domain,'_split_ids.csv'))
if(is.null(domain)||!(domain %in% c('uci','ulb'))) stop('Required --domain uci|ulb')
for (name in c('digest','jsonlite')) if(!requireNamespace(name,quietly=TRUE)) stop('Missing R dependency: ',name)
if(domain=='uci'&&!requireNamespace('readxl',quietly=TRUE)) stop('Missing R dependency readxl')
if(!file.exists(path)||!file.exists(splits_path)) stop('Verified raw data and frozen Python split CSV required')
expected<-if(domain=='uci') '30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933' else '76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89'
sha<-digest::digest(file=path,algo='sha256')
split_sha<-digest::digest(file=splits_path,algo='sha256')
if(sha!=expected) stop('Dataset checksum mismatch: ',sha)
if(domain=='uci') {
  df<-as.data.frame(readxl::read_excel(path,sheet='Data',skip=1))
  if(anyNA(df$ID)||anyDuplicated(df$ID))stop('Invalid UCI identifiers')
  df$row_id<-sprintf('uci-%06d',as.integer(df$ID))
  features<-c('LIMIT_BAL','EDUCATION','MARRIAGE',paste0('PAY_',c(0,2,3,4,5,6)),paste0('BILL_AMT',1:6),paste0('PAY_AMT',1:6))
  target<-'default payment next month'
  if(nrow(df)!=30000||sum(df[[target]]==1)!=6636) stop('Unexpected UCI data size')
} else {
  df<-read.csv(path,check.names=FALSE)
  df$row_id<-sprintf('ulb-%06d',seq_len(nrow(df))-1L)
  features<-c(paste0('V',1:28),'Amount');target<-'Class'
  if(nrow(df)!=284807||sum(df[[target]]==1)!=492)stop('Unexpected ULB data size')
}
if(!all(features %in% names(df)))stop('Missing feature(s)')
if(anyNA(df[,c(features,target),drop=FALSE]))stop('Unexpected missing cells')
parts<-read.csv(splits_path,colClasses='character',check.names=FALSE)
if(nrow(parts)!=nrow(df)||!identical(parts$row_id,df$row_id))stop('row_id/split mismatch')
if(!setequal(unique(parts$split),c('train','validation','test')))stop('Unexpected split names')
X<-as.matrix(df[,features,drop=FALSE]);storage.mode(X)<-'double'
if(!all(is.finite(X)))stop('Non-finite features')
tr<-which(parts$split=='train');val<-which(parts$split=='validation');test<-which(parts$split=='test')
center<-colMeans(X[tr,,drop=FALSE]);scale<-apply(X[tr,,drop=FALSE],2,sd)
scale[!is.finite(scale)|scale==0]<-1
z<-sweep(sweep(X,2,center,'-'),2,scale,'/')
fit<-glm.fit(x=cbind('(Intercept)'=1,z[tr,,drop=FALSE]),y=as.integer(df[[target]][tr]),
             family=binomial(),control=glm.control(maxit=150,epsilon=1e-9))
if(!isTRUE(fit$converged)||any(!is.finite(fit$coefficients))) stop('GLM failed to converge')
dir.create(package,recursive=TRUE,showWarnings=FALSE)
# Keep only the fitted predictor parameters. glm.fit also carries per-training-row
# residuals, fitted.values, effects, weights and QR arrays; these are NOT needed
# for inference and made the fraud model ~43 MB. Preserve the legacy fit$coefficients
# access path so the existing score.R works for compact and older RDS packages.
compact_fit<-list(coefficients=fit$coefficients)
saveRDS(list(fit=compact_fit,features=features,center=center,scale=scale,domain=domain,
             checksum=sha,split_sha256=split_sha,version='1.0.0'),file.path(package,'model.rds'))
predict_rows<-function(ix) {
  as.numeric(plogis(cbind(1,z[ix,,drop=FALSE]) %*% fit$coefficients))
}
pval<-predict_rows(val)
# Select F1 using validation only.
ts<-sort(unique(pval))
f1<-function(t) {
 pred<-pval>=t;y<-df[[target]][val];tp<-sum(pred&y==1);fp<-sum(pred&y==0);fn<-sum(!pred&y==1)
 if(2*tp+fp+fn==0) 0 else 2*tp/(2*tp+fp+fn)
}
threshold<-ts[which.max(vapply(ts,f1,numeric(1)))]
# Convert centred/scaled R coefficients to RAW predictor-unit coefficients.
coefs<-as.numeric(fit$coefficients[-1]/scale)
intercept<-as.numeric(fit$coefficients[1]-sum(center*coefs))
jsonlite::write_json(list(feature_order=features,coefficients=coefs,intercept=intercept,
           parity_atol=1e-8,model_version='1.0.0',source='model.rds'),
           file.path(package,'r_coefficients.json'),auto_unbox=TRUE,pretty=TRUE,digits=17)
dir.create(file.path(package,'fixtures'),showWarnings=FALSE)
ix<-head(val,100)
write.csv(df[ix,c('row_id',features),drop=FALSE],file.path(package,'fixtures/development_inputs.csv'),row.names=FALSE)
write.csv(data.frame(row_id=df$row_id[ix],positive_probability=predict_rows(ix)),
          file.path(package,'fixtures/reference_predictions.csv'),row.names=FALSE)
jsonlite::write_json(list(execution_status='executed_full_data',data_sha256=sha,seed=20261104,
 threshold_validation_f1=as.numeric(threshold),split_sha256=split_sha,train_n=length(tr),train_positive=sum(df[[target]][tr]),
 validation_n=length(val),validation_positive=sum(df[[target]][val]),
 test_n=length(test),test_positive=sum(df[[target]][test]),
 note='Independent R evaluation metrics must be computed and reviewed; no numerical parity proof in this file'),
 file.path(package,'train_execution.json'),auto_unbox=TRUE,pretty=TRUE)
manifest<-jsonlite::fromJSON(file.path(package,'manifest.json'))
manifest$status<-'executed_full_data';manifest$artifact_paths<-'model.rds'
manifest$artifact_sha256<-digest::digest(file=file.path(package,'model.rds'),algo='sha256')
manifest$dataset_sha256_observed_this_run<-sha
manifest$frozen_split_sha256<-split_sha
manifest$fixtures<-c('fixtures/development_inputs.csv','fixtures/reference_predictions.csv')
jsonlite::write_json(manifest,file.path(package,'manifest.json'),auto_unbox=TRUE,pretty=TRUE,null='null')
cat('R GLM trained; artifact=',file.path(package,'model.rds'),'\n')
